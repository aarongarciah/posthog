import re
from datetime import timedelta
from typing import Any, List, Optional, Union, cast

from dateutil.parser import isoparse
from django.utils.timezone import now
from pydantic import BaseModel

from posthog.constants import AUTOCAPTURE_EVENT, PropertyOperatorType
from posthog.hogql import ast
from posthog.hogql.base import AST
from posthog.hogql.functions import HOGQL_AGGREGATIONS
from posthog.hogql.errors import NotImplementedException
from posthog.hogql.parser import parse_expr
from posthog.hogql.visitor import TraversingVisitor, CloningVisitor
from posthog.models import Action, ActionStep, Cohort, Property, Team, PropertyDefinition
from posthog.models.event import Selector
from posthog.models.property import PropertyGroup
from posthog.models.property.util import build_selector_regex
from posthog.models.property_definition import PropertyType
from posthog.schema import PropertyOperator, HogQLFilters
from posthog.utils import relative_date_parse


def has_aggregation(expr: AST) -> bool:
    finder = AggregationFinder()
    finder.visit(expr)
    return finder.has_aggregation


class AggregationFinder(TraversingVisitor):
    def __init__(self):
        super().__init__()
        self.has_aggregation = False

    def visit(self, node):
        if self.has_aggregation:
            return
        else:
            super().visit(node)

    def visit_select_query(self, node: ast.SelectQuery):
        # don't care about aggregations in subqueries
        pass

    def visit_call(self, node: ast.Call):
        if node.name in HOGQL_AGGREGATIONS:
            self.has_aggregation = True
        else:
            for arg in node.args:
                self.visit(arg)


def property_to_expr(property: Union[BaseModel, PropertyGroup, Property, dict, list], team: Team) -> ast.Expr:
    if isinstance(property, dict):
        property = Property(**property)
    elif isinstance(property, list):
        properties = [property_to_expr(p, team) for p in property]
        if len(properties) == 0:
            return ast.Constant(value=True)
        if len(properties) == 1:
            return properties[0]
        return ast.And(exprs=properties)
    elif isinstance(property, Property):
        pass
    elif isinstance(property, PropertyGroup):
        if property.type != PropertyOperatorType.AND and property.type != PropertyOperatorType.OR:
            raise NotImplementedException(f'PropertyGroup of unknown type "{property.type}"')

        if len(property.values) == 0:
            return ast.Constant(value=True)
        if len(property.values) == 1:
            return property_to_expr(property.values[0], team)

        if property.type == PropertyOperatorType.AND:
            return ast.And(exprs=[property_to_expr(p, team) for p in property.values])
        else:
            return ast.Or(exprs=[property_to_expr(p, team) for p in property.values])
    elif isinstance(property, BaseModel):
        property = Property(**property.dict())
    else:
        raise NotImplementedException(
            f"property_to_expr with property of type {type(property).__name__} not implemented"
        )

    if property.type == "hogql":
        return parse_expr(property.key)
    elif property.type == "event" or cast(Any, property.type) == "feature" or property.type == "person":
        operator = cast(Optional[PropertyOperator], property.operator) or PropertyOperator.exact
        value = property.value
        if isinstance(value, list):
            if len(value) == 0:
                return ast.Constant(value=True)
            elif len(value) == 1:
                value = value[0]
            else:
                exprs = [
                    property_to_expr(
                        Property(type=property.type, key=property.key, operator=property.operator, value=v), team
                    )
                    for v in value
                ]
                if (
                    operator == PropertyOperator.is_not
                    or operator == PropertyOperator.not_icontains
                    or operator == PropertyOperator.not_regex
                ):
                    return ast.And(exprs=exprs)
                return ast.Or(exprs=exprs)

        chain = ["person", "properties"] if property.type == "person" else ["properties"]
        field = ast.Field(chain=chain + [property.key])

        if operator == PropertyOperator.is_set:
            return ast.CompareOperation(op=ast.CompareOperationOp.NotEq, left=field, right=ast.Constant(value=None))
        elif operator == PropertyOperator.is_not_set:
            return ast.CompareOperation(op=ast.CompareOperationOp.Eq, left=field, right=ast.Constant(value=None))
        elif operator == PropertyOperator.icontains:
            return ast.CompareOperation(
                op=ast.CompareOperationOp.ILike,
                left=field,
                right=ast.Constant(value=f"%{value}%"),
            )
        elif operator == PropertyOperator.not_icontains:
            return ast.CompareOperation(
                op=ast.CompareOperationOp.NotILike,
                left=field,
                right=ast.Constant(value=f"%{value}%"),
            )
        elif operator == PropertyOperator.regex:
            return ast.Call(name="match", args=[field, ast.Constant(value=value)])
        elif operator == PropertyOperator.not_regex:
            return ast.Call(name="not", args=[ast.Call(name="match", args=[field, ast.Constant(value=value)])])
        elif operator == PropertyOperator.exact or operator == PropertyOperator.is_date_exact:
            op = ast.CompareOperationOp.Eq
        elif operator == PropertyOperator.is_not:
            op = ast.CompareOperationOp.NotEq
        elif operator == PropertyOperator.lt or operator == PropertyOperator.is_date_before:
            op = ast.CompareOperationOp.Lt
        elif operator == PropertyOperator.gt or operator == PropertyOperator.is_date_after:
            op = ast.CompareOperationOp.Gt
        elif operator == PropertyOperator.lte:
            op = ast.CompareOperationOp.LtEq
        elif operator == PropertyOperator.gte:
            op = ast.CompareOperationOp.GtEq
        else:
            raise NotImplementedException(f"PropertyOperator {operator} not implemented")

        # For Boolean and untyped properties, treat "true" and "false" as boolean values
        if (
            op == ast.CompareOperationOp.Eq
            or op == ast.CompareOperationOp.NotEq
            and team is not None
            and (value == "true" or value == "false")
        ):
            property_types = PropertyDefinition.objects.filter(
                team=team,
                name=property.key,
                type=PropertyDefinition.Type.PERSON if property.type == "person" else PropertyDefinition.Type.EVENT,
            )[0:1].values_list("property_type", flat=True)
            property_type = property_types[0] if property_types else None

            if not property_type or property_type == PropertyType.Boolean:
                if value == "true":
                    value = True
                if value == "false":
                    value = False

        return ast.CompareOperation(op=op, left=field, right=ast.Constant(value=value))

    elif property.type == "element":
        value = property.value
        operator = cast(Optional[PropertyOperator], property.operator) or PropertyOperator.exact
        if isinstance(value, list):
            if len(value) == 1:
                value = value[0]
            else:
                exprs = [
                    property_to_expr(
                        Property(type=property.type, key=property.key, operator=property.operator, value=v), team
                    )
                    for v in value
                ]
                if (
                    operator == PropertyOperator.is_not
                    or operator == PropertyOperator.not_icontains
                    or operator == PropertyOperator.not_regex
                ):
                    return ast.And(exprs=exprs)
                return ast.Or(exprs=exprs)

        if property.key == "selector" or property.key == "tag_name":
            if operator != PropertyOperator.exact and operator != PropertyOperator.is_not:
                raise NotImplementedException(
                    f"property_to_expr for element {property.key} only supports exact and is_not operators, not {operator}"
                )
            expr = selector_to_expr(str(value)) if property.key == "selector" else tag_name_to_expr(str(value))
            if operator == PropertyOperator.is_not:
                return ast.Call(name="not", args=[expr])
            return expr

        if property.key == "href":
            return element_chain_key_filter("href", str(value), operator)

        if property.key == "text":
            return element_chain_key_filter("text", str(value), operator)

        raise NotImplementedException(f"property_to_expr for type element not implemented for key {property.key}")
    elif property.type == "cohort" or property.type == "static-cohort" or property.type == "precalculated-cohort":
        if not team:
            raise Exception("Can not convert cohort property to expression without team")

        cohort = Cohort.objects.get(team=team, id=property.value)
        return ast.CompareOperation(
            left=ast.Field(chain=["person_id"]),
            op=ast.CompareOperationOp.InCohort,
            right=ast.Constant(value=cohort.pk),
        )

    # TODO: Add support for these types "group", "recording", "behavioral", and "session" types

    raise NotImplementedException(f"property_to_expr not implemented for filter type {type(property).__name__}")


def action_to_expr(action: Action) -> ast.Expr:
    steps = action.steps.all()

    if len(steps) == 0:
        return ast.Constant(value=True)

    or_queries = []
    for step in steps:
        exprs: List[ast.Expr] = []
        if step.event:
            exprs.append(parse_expr("event = {event}", {"event": ast.Constant(value=step.event)}))

        if step.event == AUTOCAPTURE_EVENT:
            if step.selector:
                exprs.append(selector_to_expr(step.selector))
            if step.tag_name is not None:
                exprs.append(tag_name_to_expr(step.tag_name))
            if step.href is not None:
                if step.href_matching == ActionStep.REGEX:
                    operator = PropertyOperator.regex
                elif step.href_matching == ActionStep.CONTAINS:
                    operator = PropertyOperator.icontains
                else:
                    operator = PropertyOperator.exact
                exprs.append(element_chain_key_filter("href", step.href, operator))
            if step.text is not None:
                if step.text_matching == ActionStep.REGEX:
                    operator = PropertyOperator.regex
                elif step.text_matching == ActionStep.CONTAINS:
                    operator = PropertyOperator.icontains
                else:
                    operator = PropertyOperator.exact
                exprs.append(element_chain_key_filter("text", step.text, operator))

        if step.url:
            if step.url_matching == ActionStep.EXACT:
                expr = parse_expr("properties.$current_url = {url}", {"url": ast.Constant(value=step.url)})
            elif step.url_matching == ActionStep.REGEX:
                expr = parse_expr("properties.$current_url =~ {regex}", {"regex": ast.Constant(value=step.url)})
            else:
                expr = parse_expr("properties.$current_url like {url}", {"url": ast.Constant(value=f"%{step.url}%")})
            exprs.append(expr)

        if step.properties:
            exprs.append(property_to_expr(step.properties, action.team))

        if len(exprs) == 1:
            or_queries.append(exprs[0])
        elif len(exprs) > 1:
            or_queries.append(ast.And(exprs=exprs))
        else:
            or_queries.append(ast.Constant(value=True))

    if len(or_queries) == 1:
        return or_queries[0]
    else:
        return ast.Or(exprs=or_queries)


def element_chain_key_filter(key: str, text: str, operator: PropertyOperator):
    escaped = text.replace('"', r"\"")
    if operator == PropertyOperator.is_set or operator == PropertyOperator.is_not_set:
        value = r'[^"]+'
    elif operator == PropertyOperator.icontains or operator == PropertyOperator.not_icontains:
        value = rf'[^"]*{re.escape(escaped)}[^"]*'
    elif operator == PropertyOperator.regex or operator == PropertyOperator.not_regex:
        value = escaped
    elif operator == PropertyOperator.exact or operator == PropertyOperator.is_not:
        value = re.escape(escaped)
    else:
        raise NotImplementedException(f"element_href_to_expr not implemented for operator {operator}")

    regex = f'({key}="{value}")'
    if operator == PropertyOperator.icontains or operator == PropertyOperator.not_icontains:
        expr = parse_expr("elements_chain =~* {regex}", {"regex": ast.Constant(value=str(regex))})
    else:
        expr = parse_expr("elements_chain =~ {regex}", {"regex": ast.Constant(value=str(regex))})

    if (
        operator == PropertyOperator.is_not_set
        or operator == PropertyOperator.not_icontains
        or operator == PropertyOperator.is_not
        or operator == PropertyOperator.not_regex
    ):
        expr = ast.Call(name="not", args=[expr])
    return expr


def tag_name_to_expr(tag_name: str):
    regex = rf"(^|;){tag_name}(\.|$|;|:)"
    expr = parse_expr("elements_chain =~ {regex}", {"regex": ast.Constant(value=str(regex))})
    return expr


def selector_to_expr(selector: str):
    regex = build_selector_regex(Selector(selector, escape_slashes=False))
    expr = parse_expr("elements_chain =~ {regex}", {"regex": ast.Constant(value=regex)})
    return expr


def replace_filters(node: ast.Expr, filters: HogQLFilters, team: Team) -> ast.Expr:
    return ReplaceFilters(filters, team).visit(node)


class ReplaceFilters(CloningVisitor):
    def __init__(self, filters: HogQLFilters, team: Team = None):
        super().__init__()
        self.filters = filters
        self.team = team
        self.scopes = []

    def visit_select_query(self, node):
        self.scopes.append(node)
        node = super().visit_select_query(node)
        self.scopes.pop()
        return node

    def visit_placeholder(self, node):
        # TODO: throw if using this on any table that is not events
        if node.field == "filters":
            if self.filters is None:
                return ast.Constant(value=True)
            exprs: List[ast.Expr] = []
            if self.filters.properties is not None:
                exprs.append(property_to_expr(self.filters.properties, self.team))

            dateTo = self.filters.dateTo or (now() + timedelta(seconds=5)).isoformat()
            try:
                parsed_date = isoparse(dateTo)
            except ValueError:
                parsed_date = relative_date_parse(dateTo, self.team.timezone_info)
            exprs.append(parse_expr("timestamp < {timestamp}", {"timestamp": ast.Constant(value=parsed_date)}))

            # limit to the last 7d by default
            dateFrom = self.filters.dateFrom or "-7d"
            if dateFrom != "all":
                try:
                    parsed_date = isoparse(dateFrom)
                except ValueError:
                    parsed_date = relative_date_parse(dateFrom, self.team.timezone_info)
                exprs.append(parse_expr("timestamp >= {timestamp}", {"timestamp": ast.Constant(value=parsed_date)}))

            if len(exprs) == 0:
                return ast.Constant(value=True)
            if len(exprs) == 1:
                return exprs[0]
            return ast.And(exprs=exprs)
