import dataclasses
from datetime import timedelta
from math import ceil
from typing import Literal, Optional, Any, Dict, TypedDict, cast

from django.utils.timezone import datetime
from posthog.caching.insights_api import (
    BASE_MINIMUM_INSIGHT_REFRESH_INTERVAL,
    REDUCED_MINIMUM_INSIGHT_REFRESH_INTERVAL,
)
from posthog.caching.utils import is_stale
from posthog.constants import AUTOCAPTURE_EVENT, FunnelCorrelationType
from posthog.models.element.element import chain_to_elements
from posthog.models.event.util import ElementSerializer

from posthog.hogql import ast
from posthog.hogql.constants import LimitContext
from posthog.hogql.printer import to_printed_hogql
from posthog.hogql.query import execute_hogql_query
from posthog.hogql.timings import HogQLTimings
from posthog.hogql_queries.insights.funnels.funnel_query_context import FunnelQueryContext
from posthog.hogql_queries.insights.funnels.funnel_time_to_convert import FunnelTimeToConvert
from posthog.hogql_queries.insights.funnels.funnel_trends import FunnelTrends
from posthog.hogql_queries.insights.funnels.utils import get_funnel_actor_class, get_funnel_order_class
from posthog.hogql_queries.query_runner import QueryRunner
from posthog.hogql_queries.utils.query_date_range import QueryDateRange
from posthog.models import Team
from posthog.models.filters.mixins.utils import cached_property
from posthog.queries.util import correct_result_for_sampling
from posthog.schema import (
    EventDefinition,
    FunnelCorrelationQuery,
    FunnelCorrelationResponse,
    FunnelCorrelationResult,
    FunnelVizType,
    FunnelsQuery,
    FunnelsQueryResponse,
    HogQLQueryModifiers,
)


class EventOddsRatio(TypedDict):
    event: str

    success_count: int
    failure_count: int

    odds_ratio: float
    correlation_type: Literal["success", "failure"]


@dataclasses.dataclass
class EventStats:
    success_count: int
    failure_count: int


@dataclasses.dataclass
class EventContingencyTable:
    """
    Represents a contingency table for a single event. Note that this isn't a
    complete contingency table, but rather only includes totals for
    failure/success as opposed to including the number of successes for cases
    that a persons _doesn't_ visit an event.
    """

    event: str
    visited: EventStats

    success_total: int
    failure_total: int


MIN_PERSON_COUNT = 25
MIN_PERSON_PERCENTAGE = 0.02
PRIOR_COUNT = 1


class FunnelCorrelationQueryRunner(QueryRunner):
    TOTAL_IDENTIFIER = "Total_Values_In_Query"
    ELEMENTS_DIVIDER = "__~~__"
    AUTOCAPTURE_EVENT_TYPE = "$event_type"

    query: FunnelCorrelationQuery
    query_type = FunnelCorrelationQuery
    # context: FunnelQueryContext

    def __init__(
        self,
        query: FunnelCorrelationQuery | Dict[str, Any],
        team: Team,
        timings: Optional[HogQLTimings] = None,
        modifiers: Optional[HogQLQueryModifiers] = None,
        limit_context: Optional[LimitContext] = None,
    ):
        super().__init__(query, team=team, timings=timings, modifiers=modifiers, limit_context=limit_context)

        # self.context = FunnelQueryContext(
        #     query=self.query, team=team, timings=timings, modifiers=modifiers, limit_context=limit_context
        # )

        # if self._filter.funnel_step is None:
        #     self._filter = self._filter.shallow_clone({"funnel_step": 1})
        #     # Funnel Step by default set to 1, to give us all people who entered the funnel

        # # Used for generating the funnel persons cte

        # filter_data = {
        #     key: value
        #     for key, value in self._filter.to_dict().items()
        #     # NOTE: we want to filter anything about correlation, as the
        #     # funnel persons endpoint does not understand or need these
        #     # params.
        #     if not key.startswith("funnel_correlation_")
        # }
        # # NOTE: we always use the final matching event for the recording because this
        # # is the the right event for both drop off and successful funnels
        # filter_data.update({"include_final_matching_events": self._filter.include_recordings})
        # filter = Filter(data=filter_data, hogql_context=self._filter.hogql_context)

        # funnel_order_actor_class = get_funnel_order_actor_class(filter)

        # self._funnel_actors_generator = funnel_order_actor_class(
        #     filter,
        #     self._team,
        #     # NOTE: we want to include the latest timestamp of the `target_step`,
        #     # from this we can deduce if the person reached the end of the funnel,
        #     # i.e. successful
        #     include_timestamp=True,
        #     # NOTE: we don't need these as we have all the information we need to
        #     # deduce if the person was successful or not
        #     include_preceding_timestamp=False,
        #     include_properties=self.properties_to_include,
        # )

    # def _is_stale(self, cached_result_package):
    #     date_to = self.query_date_range.date_to()
    #     interval = self.query_date_range.interval_name
    #     return is_stale(self.team, date_to, interval, cached_result_package)
    def _is_stale(self, cached_result_package):
        return True

    # def _refresh_frequency(self):
    #     date_to = self.query_date_range.date_to()
    #     date_from = self.query_date_range.date_from()
    #     interval = self.query_date_range.interval_name
    def _refresh_frequency(self):
        return timedelta(minutes=1)

    #     delta_days: Optional[int] = None
    #     if date_from and date_to:
    #         delta = date_to - date_from
    #         delta_days = ceil(delta.total_seconds() / timedelta(days=1).total_seconds())

    #     refresh_frequency = BASE_MINIMUM_INSIGHT_REFRESH_INTERVAL
    #     if interval == "hour" or (delta_days is not None and delta_days <= 7):
    #         # The interval is shorter for short-term insights
    #         refresh_frequency = REDUCED_MINIMUM_INSIGHT_REFRESH_INTERVAL

    #     return refresh_frequency

    def calculate(self) -> FunnelCorrelationResponse:
        """
        Funnel Correlation queries take as input the same as the funnel query,
        and returns the correlation of person events with a person successfully
        getting to the end of the funnel. We use Odds Ratios as the correlation
        metric. See https://en.wikipedia.org/wiki/Odds_ratio for more details.

        Roughly speaking, to calculate the odds ratio, we build a contingency
        table https://en.wikipedia.org/wiki/Contingency_table for each
        dimension, then calculate the odds ratio for each.

        For example, take for simplicity the cohort of all people, and the
        success criteria of having a "signed up" event. First we would build a
        contingency table like:

        |                    | success | failure | total |
        | -----------------: | :-----: | :-----: | :---: |
        | watched video      |    5    |    1    |   6   |
        | didn't watch video |    2    |   10    |   12  |


        Then the odds that a person signs up given they watched the video is 5 /
        1.

        And the odds that a person signs up given they didn't watch the video is
        2 / 10.

        So we say the odds ratio is 5 / 1 over 2 / 10 = 25 . The further away the
        odds ratio is from 1, the greater the correlation.

        Requirements:

         - Intitially we only need to consider the names of events that a cohort
           person has emitted. So we explicitly are not interested in e.g.
           correlating properties, although this will be a follow-up.

        Non-functional requirements:

         - there can be perhaps millions of people in a cohort, so we should
           consider this when writing the algorithm. e.g. we should probably
           avoid pulling all people into across the wire.
         - there can be an order of magnitude more events than people, so we
           should avoid pulling all events across the wire.
         - there may be a large but not huge number of distinct events, let's say
           100 different names for events. We should avoid n+1 queries for the
           event names dimension

        Contincency tables are something we can pull out of the db, so we can
        have a query that:

         1. filters people by the cohort criteria
         2. groups these people by the success criteria
         3. groups people by our criterion with which we want to test
            correlation, e.g. "watched video"

        --

        For each event a person that started going through the funnel, gets stats
        for how many of these users are sucessful and how many are unsuccessful.

        It's a partial table as it doesn't include numbers of the negation of the
        event, but does include the total success/failure numbers, which is enough
        for us to calculate the odds ratio.
        """
        if not self.query.source.series:
            return FunnelCorrelationResponse(result=FunnelCorrelationResult(events=[], skewed=False))

        query = self.to_query()

        hogql = to_printed_hogql(query, self.team)

        response = execute_hogql_query(
            query_type="FunnelsQuery",
            query=query,
            team=self.team,
            timings=self.timings,
            modifiers=self.modifiers,
        )

        # Get the total success/failure counts from the results
        results = [result for result in response if result[0] != self.TOTAL_IDENTIFIER]
        _, success_total, failure_total = [result for result in response if result[0] == self.TOTAL_IDENTIFIER][0]

        # Add a little structure, and keep it close to the query definition so it's
        # obvious what's going on with result indices.
        event_contingency_tables = (
            [
                EventContingencyTable(
                    event=result[0],
                    visited=EventStats(success_count=result[1], failure_count=result[2]),
                    success_total=success_total,
                    failure_total=failure_total,
                )
                for result in results
            ],
        )

        success_total = int(correct_result_for_sampling(success_total, self.query.source.samplingFactor))
        failure_total = int(correct_result_for_sampling(failure_total, self.query.source.samplingFactor))

        if not success_total or not failure_total:
            return FunnelCorrelationResponse(result=FunnelCorrelationResult(events=[], skewed=True))

        skewed_totals = False

        # If the ratio is greater than 1:10, then we have a skewed result, so we should
        # warn the user.
        if success_total / failure_total > 10 or failure_total / success_total > 10:
            skewed_totals = True

        odds_ratios = [
            get_entity_odds_ratio(event_stats, PRIOR_COUNT)
            for event_stats in event_contingency_tables
            if not are_results_insignificant(event_stats)
        ]

        positively_correlated_events = sorted(
            [odds_ratio for odds_ratio in odds_ratios if odds_ratio["correlation_type"] == "success"],
            key=lambda x: x["odds_ratio"],
            reverse=True,
        )

        negatively_correlated_events = sorted(
            [odds_ratio for odds_ratio in odds_ratios if odds_ratio["correlation_type"] == "failure"],
            key=lambda x: x["odds_ratio"],
            reverse=False,
        )

        # Return the top ten positively correlated events, and top then negatively correlated events
        events = positively_correlated_events[:10] + negatively_correlated_events[:10]

        return FunnelCorrelationResponse(
            result=FunnelCorrelationResult(
                events=[self.serialize_event_odds_ratio(odds_ratio=odds_ratio) for odds_ratio in events],
                skewed=skewed_totals,
            ),
            timings=response.timings,
            hogql=hogql,
        )

    def serialize_event_odds_ratio(self, odds_ratio: EventOddsRatio) -> EventOddsRatioSerialized:
        event_definition = self.serialize_event_with_property(event=odds_ratio["event"])
        return {
            "success_count": odds_ratio["success_count"],
            # "success_people_url": self.construct_people_url(
            #     success=True,
            #     event_definition=event_definition,
            #     cache_invalidation_key=cache_invalidation_key,
            # ),
            "failure_count": odds_ratio["failure_count"],
            # "failure_people_url": self.construct_people_url(
            #     success=False,
            #     event_definition=event_definition,
            #     cache_invalidation_key=cache_invalidation_key,
            # ),
            "odds_ratio": odds_ratio["odds_ratio"],
            "correlation_type": odds_ratio["correlation_type"],
            "event": event_definition,
        }

    def serialize_event_with_property(self, event: str) -> EventDefinition:
        """
        Format the event name for display.
        """
        if not self.support_autocapture_elements():
            return EventDefinition(event=event, properties={}, elements=[])

        event_name, property_name, property_value = event.split("::")
        if event_name == AUTOCAPTURE_EVENT and property_name == "elements_chain":
            event_type, elements_chain = property_value.split(self.ELEMENTS_DIVIDER)
            return EventDefinition(
                event=event,
                properties={self.AUTOCAPTURE_EVENT_TYPE: event_type},
                elements=cast(
                    list,
                    ElementSerializer(chain_to_elements(elements_chain), many=True).data,
                ),
            )

        return EventDefinition(event=event, properties={}, elements=[])

    def to_query(self) -> ast.SelectQuery:
        """
        Returns a query string and params, which are used to generate the contingency table.
        The query returns success and failure count for event / property values, along with total success and failure counts.
        """
        if self.query.correlationType == FunnelCorrelationType.PROPERTIES:
            return self.get_properties_query()

        if self.query.correlationType == FunnelCorrelationType.EVENT_WITH_PROPERTIES:
            return self.get_event_property_query()

        return self.get_event_query()

    def get_event_query(self) -> ast.SelectQuery:
        funnel_persons_query, funnel_persons_params = self.get_funnel_actors_cte()

        event_join_query = self._get_events_join_query()

        query = f"""
            WITH
                funnel_actors as ({funnel_persons_query}),
                toDateTime(%(date_to)s, %(timezone)s) AS date_to,
                toDateTime(%(date_from)s, %(timezone)s) AS date_from,
                %(target_step)s AS target_step,
                %(funnel_step_names)s as funnel_step_names

            SELECT
                event.event AS name,

                -- If we have a `person.steps = target_step`, we know the person
                -- reached the end of the funnel
                countDistinctIf(
                    actors.actor_id,
                    actors.steps = target_step
                ) AS success_count,

                -- And the converse being for failures
                countDistinctIf(
                    actors.actor_id,
                    actors.steps <> target_step
                ) AS failure_count

            FROM events AS event
                {event_join_query}
                AND event.event NOT IN %(exclude_event_names)s
            GROUP BY name

            -- To get the total success/failure numbers, we do an aggregation on
            -- the funnel people CTE and count distinct actor_ids
            UNION ALL

            SELECT
                -- We're not using WITH TOTALS because the resulting queries are
                -- not runnable in Metabase
                '{self.TOTAL_IDENTIFIER}' as name,

                countDistinctIf(
                    actors.actor_id,
                    actors.steps = target_step
                ) AS success_count,

                countDistinctIf(
                    actors.actor_id,
                    actors.steps <> target_step
                ) AS failure_count
            FROM funnel_actors AS actors
        """
        params = {
            **funnel_persons_params,
            "funnel_step_names": self._get_funnel_step_names(),
            "target_step": len(self._filter.entities),
            "exclude_event_names": self._filter.correlation_event_exclude_names,
        }

        return query

    def get_event_property_query(self) -> ast.SelectQuery:
        if not self._filter.correlation_event_names:
            raise ValidationError("Event Property Correlation expects atleast one event name to run correlation on")

        funnel_persons_query, funnel_persons_params = self.get_funnel_actors_cte()

        event_join_query = self._get_events_join_query()

        if self.support_autocapture_elements():
            event_type_expression, _ = get_property_string_expr(
                "events",
                self.AUTOCAPTURE_EVENT_TYPE,
                f"'{self.AUTOCAPTURE_EVENT_TYPE}'",
                "properties",
            )
            array_join_query = f"""
                'elements_chain' as prop_key,
                concat({event_type_expression}, '{self.ELEMENTS_DIVIDER}', elements_chain) as prop_value,
                tuple(prop_key, prop_value) as prop
            """
        else:
            array_join_query = f"""
                arrayJoin(JSONExtractKeysAndValues(properties, 'String')) as prop
            """

        query = f"""
            WITH
                funnel_actors as ({funnel_persons_query}),
                toDateTime(%(date_to)s, %(timezone)s) AS date_to,
                toDateTime(%(date_from)s, %(timezone)s) AS date_from,
                %(target_step)s AS target_step,
                %(funnel_step_names)s as funnel_step_names

            SELECT concat(event_name, '::', prop.1, '::', prop.2) as name,
                   countDistinctIf(actor_id, steps = target_step) as success_count,
                   countDistinctIf(actor_id, steps <> target_step) as failure_count
            FROM (
                SELECT
                    actors.actor_id as actor_id,
                    actors.steps as steps,
                    events.event as event_name,
                    -- Same as what we do in $all property queries
                    {array_join_query}
                FROM events AS event
                    {event_join_query}
                    AND event.event IN %(event_names)s
            )
            GROUP BY name
            -- Discard high cardinality / low hits properties
            -- This removes the long tail of random properties with empty, null, or very small values
            HAVING (success_count + failure_count) > 2
            AND prop.1 NOT IN %(exclude_property_names)s

            UNION ALL
            -- To get the total success/failure numbers, we do an aggregation on
            -- the funnel people CTE and count distinct actor_ids
            SELECT
                '{self.TOTAL_IDENTIFIER}' as name,

                countDistinctIf(
                    actors.actor_id,
                    actors.steps = target_step
                ) AS success_count,

                countDistinctIf(
                    actors.actor_id,
                    actors.steps <> target_step
                ) AS failure_count
            FROM funnel_actors AS actors
        """
        params = {
            **funnel_persons_params,
            "funnel_step_names": self._get_funnel_step_names(),
            "target_step": len(self._filter.entities),
            "event_names": self._filter.correlation_event_names,
            "exclude_property_names": self._filter.correlation_event_exclude_property_names,
        }

        return query, params

    def get_properties_query(self) -> ast.SelectQuery:
        if not self._filter.correlation_property_names:
            raise ValidationError("Property Correlation expects atleast one Property to run correlation on")

        funnel_actors_query, funnel_actors_params = self.get_funnel_actors_cte()

        person_prop_query, person_prop_params = self._get_properties_prop_clause()

        (
            aggregation_join_query,
            aggregation_join_params,
        ) = self._get_aggregation_join_query()

        query = f"""
            WITH
                funnel_actors as ({funnel_actors_query}),
                %(target_step)s AS target_step
            SELECT
                concat(prop.1, '::', prop.2) as name,
                -- We generate a unique identifier for each property value as: PropertyName::Value
                countDistinctIf(actor_id, steps = target_step) AS success_count,
                countDistinctIf(actor_id, steps <> target_step) AS failure_count
            FROM (
                SELECT
                    actor_id,
                    funnel_actors.steps as steps,
                    /*
                        We can extract multiple property values at the same time, since we're
                        already querying the person table.
                        This gives us something like:
                        --------------------
                        person1, steps, [property_value_0, property_value_1, property_value_2]
                        person2, steps, [property_value_0, property_value_1, property_value_2]

                        To group by property name, we need to extract the property from the array. ArrayJoin helps us do that.
                        It transforms the above into:

                        --------------------

                        person1, steps, property_value_0
                        person1, steps, property_value_1
                        person1, steps, property_value_2

                        person2, steps, property_value_0
                        person2, steps, property_value_1
                        person2, steps, property_value_2

                        To avoid clashes and clarify the values, we also zip with the property name, to generate
                        tuples like: (property_name, property_value), which we then group by
                    */
                    {person_prop_query}
                FROM funnel_actors
                {aggregation_join_query}

            ) aggregation_target_with_props
            -- Group by the tuple items: (property_name, property_value) generated by zip
            GROUP BY prop.1, prop.2
            HAVING prop.1 NOT IN %(exclude_property_names)s
            UNION ALL
            SELECT
                '{self.TOTAL_IDENTIFIER}' as name,
                countDistinctIf(actor_id, steps = target_step) AS success_count,
                countDistinctIf(actor_id, steps <> target_step) AS failure_count
            FROM funnel_actors
        """
        params = {
            **funnel_actors_params,
            **person_prop_params,
            **aggregation_join_params,
            "target_step": len(self._filter.entities),
            "property_names": self._filter.correlation_property_names,
            "exclude_property_names": self._filter.correlation_property_exclude_names,
        }

        return query, params

    def get_funnel_actors_cte(self) -> Tuple[str, Dict[str, Any]]:
        extra_fields = ["steps", "final_timestamp", "first_timestamp"]

        for prop in self.properties_to_include:
            extra_fields.append(prop)

        return self._funnel_actors_generator.actor_query(limit_actors=False, extra_fields=extra_fields)

    def _get_events_join_query(self) -> str:
        """
        This query is used to join and filter the events table corresponding to the funnel_actors CTE.
        It expects the following variables to be present in the CTE expression:
            - funnel_actors
            - date_to
            - date_from
            - funnel_step_names
        """

        return f"""
            {self._get_aggregation_target_join_query()}

            -- Make sure we're only looking at events before the final step, or
            -- failing that, date_to
            WHERE
                -- add this condition in to ensure we can filter events before
                -- joining funnel_actors
                toTimeZone(toDateTime(event.timestamp), 'UTC') >= date_from
                AND toTimeZone(toDateTime(event.timestamp), 'UTC') < date_to

                AND event.team_id = {self._team.pk}

                -- Add in per actor filtering on event time range. We just want
                -- to include events that happened within the bounds of the
                -- actors time in the funnel.
                AND toTimeZone(toDateTime(event.timestamp), 'UTC') > actors.first_timestamp
                AND toTimeZone(toDateTime(event.timestamp), 'UTC') < COALESCE(
                    actors.final_timestamp,
                    actors.first_timestamp + INTERVAL {self._funnel_actors_generator._filter.funnel_window_interval} {self._funnel_actors_generator._filter.funnel_window_interval_unit_ch()},
                    date_to)
                    -- Ensure that the event is not outside the bounds of the funnel conversion window

                -- Exclude funnel steps
                AND event.event NOT IN funnel_step_names
        """

    def _get_aggregation_target_join_query(self) -> str:
        if self._team.person_on_events_mode == PersonOnEventsMode.V1_ENABLED:
            aggregation_person_join = f"""
                JOIN funnel_actors as actors
                    ON event.person_id = actors.actor_id
            """

        else:
            aggregation_person_join = f"""
                JOIN ({get_team_distinct_ids_query(self._team.pk)}) AS pdi
                        ON pdi.distinct_id = events.distinct_id

                    -- NOTE: I would love to right join here, so we count get total
                    -- success/failure numbers in one pass, but this causes out of memory
                    -- error mentioning issues with right filling. I'm sure there's a way
                    -- to do it but lifes too short.
                    JOIN funnel_actors AS actors
                        ON pdi.person_id = actors.actor_id
                """

        aggregation_group_join = f"""
            JOIN funnel_actors AS actors
                ON actors.actor_id = events.$group_{self._filter.aggregation_group_type_index}
            """

        return (
            aggregation_group_join if self._filter.aggregation_group_type_index is not None else aggregation_person_join
        )

    def _get_funnel_step_names(self):
        events: Set[Union[int, str]] = set()
        for entity in self._filter.entities:
            if entity.type == TREND_FILTER_TYPE_ACTIONS:
                action = entity.get_action()
                events.update(action.get_step_events())
            elif entity.id is not None:
                events.add(entity.id)

        return sorted(list(events))

    @property
    def properties_to_include(self) -> List[str]:
        props_to_include = []
        if (
            self._team.person_on_events_mode != PersonOnEventsMode.DISABLED
            and self._filter.correlation_type == FunnelCorrelationType.PROPERTIES
        ):
            # When dealing with properties, make sure funnel response comes with properties
            # so we don't have to join on persons/groups to get these properties again
            mat_event_cols = get_materialized_columns("events")

            for property_name in cast(list, self._filter.correlation_property_names):
                if self._filter.aggregation_group_type_index is not None:
                    if not groups_on_events_querying_enabled():
                        continue

                    if "$all" == property_name:
                        return [f"group{self._filter.aggregation_group_type_index}_properties"]

                    possible_mat_col = mat_event_cols.get(
                        (
                            property_name,
                            f"group{self._filter.aggregation_group_type_index}_properties",
                        )
                    )
                    if possible_mat_col is not None:
                        props_to_include.append(possible_mat_col)
                    else:
                        props_to_include.append(f"group{self._filter.aggregation_group_type_index}_properties")

                else:
                    if "$all" == property_name:
                        return [f"person_properties"]

                    possible_mat_col = mat_event_cols.get((property_name, "person_properties"))

                    if possible_mat_col is not None:
                        props_to_include.append(possible_mat_col)
                    else:
                        props_to_include.append(f"person_properties")

        return props_to_include

    def support_autocapture_elements(self) -> bool:
        if (
            self.query.correlationType == FunnelCorrelationType.EVENT_WITH_PROPERTIES
            and AUTOCAPTURE_EVENT in self._filter.correlation_event_names
        ):
            return True
        return False


def get_entity_odds_ratio(event_contingency_table: EventContingencyTable, prior_counts: int) -> EventOddsRatio:
    # Add 1 to all values to prevent divide by zero errors, and introduce a [prior](https://en.wikipedia.org/wiki/Prior_probability)
    odds_ratio = (
        (event_contingency_table.visited.success_count + prior_counts)
        * (event_contingency_table.failure_total - event_contingency_table.visited.failure_count + prior_counts)
    ) / (
        (event_contingency_table.success_total - event_contingency_table.visited.success_count + prior_counts)
        * (event_contingency_table.visited.failure_count + prior_counts)
    )

    return EventOddsRatio(
        event=event_contingency_table.event,
        success_count=event_contingency_table.visited.success_count,
        failure_count=event_contingency_table.visited.failure_count,
        odds_ratio=odds_ratio,
        correlation_type="success" if odds_ratio > 1 else "failure",
    )


def are_results_insignificant(event_contingency_table: EventContingencyTable) -> bool:
    """
    Check if the results are insignificant, i.e. if the success/failure counts are
    significantly different from the total counts
    """

    total_count = event_contingency_table.success_total + event_contingency_table.failure_total

    if event_contingency_table.visited.success_count + event_contingency_table.visited.failure_count < min(
        MIN_PERSON_COUNT,
        MIN_PERSON_PERCENTAGE * total_count,
    ):
        return True

    return False
