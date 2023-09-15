import { HeatmapStats } from '~/toolbar/stats/HeatmapStats'
import { Spinner } from 'lib/lemon-ui/Spinner'
import { LemonButton } from 'lib/lemon-ui/LemonButton'
import {
    IconArrowDown,
    IconArrowUp,
    IconClick,
    IconClose,
    IconDarkMode,
    IconDragHandle,
    IconFlag,
    IconHelpOutline,
    IconLightMode,
    IconMagnifier,
    IconMenu,
    IconTarget,
} from 'lib/lemon-ui/icons'
import { ActionsTab } from '~/toolbar/actions/ActionsTab'
import { LemonBadge } from 'lib/lemon-ui/LemonBadge'
import { FeatureFlags } from '~/toolbar/flags/FeatureFlags'
import { LemonDivider } from 'lib/lemon-ui/LemonDivider'
import { LemonMenu } from 'lib/lemon-ui/LemonMenu'
import { getToolbarContainer } from '~/toolbar/utils'
import { Logomark as Logomark3000 } from '~/toolbar/button/icons/icons'
import { useActions, useValues } from 'kea'
import { toolbarButtonLogic } from '~/toolbar/button/toolbarButtonLogic'
import { actionsTabLogic } from '~/toolbar/actions/actionsTabLogic'
import { actionsLogic } from '~/toolbar/actions/actionsLogic'
import { elementsLogic } from '~/toolbar/elements/elementsLogic'
import { heatmapLogic } from '~/toolbar/elements/heatmapLogic'
import { toolbarLogic } from '~/toolbar/toolbarLogic'
import { HELP_URL } from './ToolbarButton'
import { useLayoutEffect, useRef } from 'react'
import { useKeyboardHotkeys } from 'lib/hooks/useKeyboardHotkeys'
import clsx from 'clsx'

function MoreMenu({
    onOpenOrClose,
}: {
    onOpenOrClose: (e: React.MouseEvent, actionFn: () => void) => void
}): JSX.Element {
    const { moreMenuVisible, theme } = useValues(toolbarButtonLogic)
    const { setHedgehogMode, closeMoreMenu, openMoreMenu, toggleTheme } = useActions(toolbarButtonLogic)

    // KLUDGE: if there is no theme, assume light mode, which shouldn't be, but seems to be, necessary
    const currentlyLightMode = !theme || theme === 'light'

    const { logout } = useActions(toolbarLogic)

    return (
        <LemonMenu
            visible={moreMenuVisible}
            onVisibilityChange={(visible) => {
                if (!visible && moreMenuVisible) {
                    closeMoreMenu()
                }
            }}
            placement={'top-start'}
            fallbackPlacements={['bottom-start']}
            getPopupContainer={getToolbarContainer}
            items={[
                {
                    icon: <>🦔</>,
                    label: 'Hedgehog mode',
                    onClick: () => {
                        setHedgehogMode(true)
                    },
                },
                {
                    icon: currentlyLightMode ? <IconDarkMode /> : <IconLightMode />,
                    label: `Switch to ${currentlyLightMode ? 'dark' : 'light'} mode`,
                    onClick: () => {
                        toggleTheme()
                    },
                },
                {
                    icon: <IconHelpOutline />,
                    label: 'Help',
                    onClick: () => {
                        window.open(HELP_URL, '_blank')?.focus()
                    },
                },
                { icon: <IconClose />, label: 'Logout', onClick: logout },
            ]}
        >
            <LemonButton
                status={'stealth'}
                icon={<IconMenu />}
                title={'More'}
                onClick={(e) => {
                    onOpenOrClose(e, moreMenuVisible ? closeMoreMenu : openMoreMenu)
                }}
                square={true}
            />
        </LemonMenu>
    )
}

/**
 * Some toolbar modes show a peek of information before opening the full menu.
 * */
function PeekMenu(): JSX.Element | null {
    const { menuPlacement, fullMenuVisible, heatmapInfoVisible, actionsInfoVisible } = useValues(toolbarButtonLogic)
    const { showHeatmapInfo, hideHeatmapInfo, showActionsInfo, hideActionsInfo } = useActions(toolbarButtonLogic)

    const { buttonActionsVisible } = useValues(actionsTabLogic)
    const { hideButtonActions } = useActions(actionsTabLogic)
    const { actionCount, allActionsLoading } = useValues(actionsLogic)

    const { heatmapEnabled, heatmapLoading, elementCount } = useValues(heatmapLogic)

    // const { countFlagsOverridden } = useValues(featureFlagsLogic)

    const peekMenuVisible = !fullMenuVisible && (heatmapEnabled || buttonActionsVisible)

    const clickHandler = heatmapEnabled
        ? heatmapInfoVisible
            ? hideHeatmapInfo
            : showHeatmapInfo
        : buttonActionsVisible
        ? actionsInfoVisible
            ? () => {
                  hideActionsInfo()
                  hideButtonActions()
              }
            : showActionsInfo
        : () => {}

    if (!peekMenuVisible) {
        return null
    } else {
        const title = heatmapEnabled ? (
            <>Heatmap: {heatmapLoading ? <Spinner textColored={true} /> : <>{elementCount} elements</>}</>
        ) : buttonActionsVisible ? (
            <>
                Actions:{' '}
                <div className="whitespace-nowrap text-center">
                    {allActionsLoading ? (
                        <Spinner textColored={true} />
                    ) : (
                        <LemonBadge.Number size={'small'} count={actionCount} showZero />
                    )}
                </div>
            </>
        ) : null

        return (
            <div
                className={
                    'flex flex-row gap-2 w-full items-center align-center justify-between px-2 pt-1 cursor-pointer'
                }
                onClick={clickHandler}
            >
                <div className={'flex flex-grow'}>
                    <h5 className={'flex flex-row items-center mb-0'}>{title}</h5>
                </div>
                <LemonButton
                    size={'small'}
                    icon={menuPlacement === 'top' ? <IconArrowUp /> : <IconArrowDown />}
                    status={'stealth'}
                    onClick={clickHandler}
                />

                {/*{flagsVisible ? (*/}
                {/*    <div className={'flex flex-grow'}>*/}
                {/*        <h5 className={'flex flex-row items-center mb-0'}>*/}
                {/*            Feature flags: {countFlagsOverridden} overridden*/}
                {/*        </h5>*/}
                {/*    </div>*/}
                {/*) : null}*/}
            </div>
        )
    }
}

function FullMenu(): JSX.Element {
    const { heatmapInfoVisible, actionsInfoVisible, flagsVisible } = useValues(toolbarButtonLogic)

    return (
        <>
            {heatmapInfoVisible ? <HeatmapStats /> : null}
            {actionsInfoVisible ? <ActionsTab /> : null}
            {flagsVisible ? <FeatureFlags /> : null}
        </>
    )
}

function ToolbarInfoMenu(): JSX.Element {
    const menuRef = useRef<HTMLDivElement | null>(null)
    const { windowHeight, dragPosition, menuPlacement, heatmapInfoVisible, actionsInfoVisible, flagsVisible } =
        useValues(toolbarButtonLogic)
    const { setMenuPlacement } = useActions(toolbarButtonLogic)
    const { heatmapEnabled } = useValues(heatmapLogic)
    const { inspectEnabled } = useValues(elementsLogic)
    const { buttonActionsVisible } = useValues(actionsTabLogic)

    useLayoutEffect(() => {
        if (!menuRef.current) {
            return
        }

        if (dragPosition.y <= 300) {
            setMenuPlacement('bottom')
        } else {
            setMenuPlacement('top')
        }

        const peekIsShowing = heatmapEnabled || buttonActionsVisible
        const fullIsShowing = heatmapInfoVisible || actionsInfoVisible || flagsVisible

        if (peekIsShowing && !fullIsShowing) {
            // needs to be a fixed value for animation to work
            menuRef.current.style.height = '40px'
        } else if (fullIsShowing) {
            let heightAvailableForMenu = menuRef.current.getBoundingClientRect().bottom
            if (menuPlacement === 'bottom') {
                heightAvailableForMenu = windowHeight - menuRef.current.getBoundingClientRect().top
            }
            menuRef.current.style.height = `${heightAvailableForMenu - 10}px`

            // TODO what if there is less than 10 available
        } else {
            menuRef.current.style.height = '0px'
        }
    }, [
        dragPosition,
        menuRef,
        heatmapInfoVisible,
        actionsInfoVisible,
        flagsVisible,
        inspectEnabled,
        heatmapEnabled,
        buttonActionsVisible,
    ])

    return (
        <div
            ref={menuRef}
            className={clsx(
                'absolute Toolbar3000 Toolbar3000__menu rounded-lg flex flex-col',
                menuPlacement === 'top' ? 'bottom' : 'top-12'
            )}
        >
            <FullMenu />
            <PeekMenu />
        </div>
    )
}

export function Toolbar3000(): JSX.Element {
    const { flagsVisible, closeTheLastOpenedMenu, minimizedWidth } = useValues(toolbarButtonLogic)
    const { showFlags, hideFlags, toggleWidth } = useActions(toolbarButtonLogic)

    const { buttonActionsVisible } = useValues(actionsTabLogic)
    const { hideButtonActions, showButtonActions } = useActions(actionsTabLogic)

    const { enableInspect, disableInspect } = useActions(elementsLogic)
    const { inspectEnabled } = useValues(elementsLogic)

    const { enableHeatmap, disableHeatmap } = useActions(heatmapLogic)
    const { heatmapEnabled } = useValues(heatmapLogic)

    const { isAuthenticated } = useValues(toolbarLogic)

    const swallowClick = (e: React.MouseEvent, actionFn: () => void): void => {
        // swallow the click
        e.preventDefault()
        e.stopPropagation()
        // close the last opened thing
        closeTheLastOpenedMenu?.()
        // carry out the action
        actionFn()
    }

    useKeyboardHotkeys(
        {
            escape: { action: () => closeTheLastOpenedMenu?.(), willHandleEvent: true },
        },
        [closeTheLastOpenedMenu]
    )

    return (
        <>
            {!minimizedWidth && <ToolbarInfoMenu />}
            <div
                className={clsx(
                    'Toolbar3000 px-2 h-10 space-x-2 rounded-lg flex flex-row items-center floating-toolbar-button',
                    minimizedWidth ? 'Toolbar3000--minimized-width' : ''
                )}
            >
                {!minimizedWidth ? (
                    <>
                        <IconDragHandle className={'text-2xl cursor-grab'} />
                        <LemonDivider vertical={true} className={'h-full bg-border-bold-3000'} />
                    </>
                ) : null}
                {isAuthenticated && !minimizedWidth ? (
                    <>
                        <LemonButton
                            title={'Inspect'}
                            icon={<IconMagnifier />}
                            status={'stealth'}
                            onClick={(e) => swallowClick(e, inspectEnabled ? disableInspect : enableInspect)}
                            active={inspectEnabled}
                            square={true}
                        />
                        <LemonButton
                            title={'Heatmap'}
                            icon={<IconClick />}
                            status={'stealth'}
                            onClick={(e) => swallowClick(e, heatmapEnabled ? disableHeatmap : enableHeatmap)}
                            active={heatmapEnabled}
                            square={true}
                        />
                        <LemonButton
                            title={'Actions'}
                            icon={<IconTarget />}
                            status={'stealth'}
                            onClick={(e) =>
                                swallowClick(e, buttonActionsVisible ? hideButtonActions : showButtonActions)
                            }
                            active={buttonActionsVisible}
                            square={true}
                        />
                        <LemonButton
                            title={'Feature flags'}
                            icon={<IconFlag />}
                            status={'stealth'}
                            onClick={(e) => swallowClick(e, flagsVisible ? hideFlags : showFlags)}
                            active={flagsVisible}
                            square={true}
                        />
                        <MoreMenu onOpenOrClose={swallowClick} />
                        <LemonDivider vertical={true} className={'h-full bg-border-bold-3000'} />
                    </>
                ) : null}
                <LemonButton
                    icon={<Logomark3000 />}
                    title={minimizedWidth ? 'expand the toolbar' : 'minimize'}
                    status={'stealth'}
                    size={'small'}
                    square={true}
                    noPadding={true}
                    onClick={(e) => {
                        e.stopPropagation()
                        toggleWidth()
                    }}
                />
            </div>
        </>
    )
}