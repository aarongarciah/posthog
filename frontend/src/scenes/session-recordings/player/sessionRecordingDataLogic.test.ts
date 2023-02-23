import {
    parseMetadataResponse,
    sessionRecordingDataLogic,
} from 'scenes/session-recordings/player/sessionRecordingDataLogic'
import { api, MOCK_TEAM_ID } from 'lib/api.mock'
import { expectLogic } from 'kea-test-utils'
import { initKeaTests } from '~/test/init'
import { eventUsageLogic } from 'lib/utils/eventUsageLogic'
import recordingSnapshotsJson from '../__mocks__/recording_snapshots.json'
import recordingMetaJson from '../__mocks__/recording_meta.json'
import recordingEventsJson from '../__mocks__/recording_events.json'
import recordingPerformanceEventsJson from '../__mocks__/recording_performance_events.json'
import { preflightLogic } from 'scenes/PreflightCheck/preflightLogic'
import { combineUrl } from 'kea-router'
import { resumeKeaLoadersErrors, silenceKeaLoadersErrors } from '~/initKea'
import { useMocks } from '~/mocks/jest'
import { teamLogic } from 'scenes/teamLogic'
import { userLogic } from 'scenes/userLogic'
import { AvailableFeature } from '~/types'
import { useAvailableFeatures } from '~/mocks/features'

const createSnapshotEndpoint = (id: number): string => `api/projects/${MOCK_TEAM_ID}/session_recordings/${id}/snapshots`
const EVENTS_SESSION_RECORDING_SNAPSHOTS_ENDPOINT_REGEX = new RegExp(
    `api/projects/${MOCK_TEAM_ID}/session_recordings/\\d/snapshots`
)
const EVENTS_SESSION_RECORDING_META_ENDPOINT = `api/projects/${MOCK_TEAM_ID}/session_recordings`
const EVENTS_SESSION_RECORDING_EVENTS_ENDPOINT = `api/projects/${MOCK_TEAM_ID}/events`
const recordingGetDataMocks = {
    '/api/projects/:team/session_recordings/:id/snapshots': recordingSnapshotsJson,
    '/api/projects/:team/session_recordings/:id': recordingMetaJson,
    '/api/projects/:team/events': { results: recordingEventsJson },
    '/api/projects/:team/performance_events': { results: recordingPerformanceEventsJson },
}

const sortedRecordingSnapshotsJson = {
    snapshot_data_by_window_id: {},
}

Object.keys(recordingSnapshotsJson.snapshot_data_by_window_id).forEach((key) => {
    sortedRecordingSnapshotsJson.snapshot_data_by_window_id[key] = [
        ...recordingSnapshotsJson.snapshot_data_by_window_id[key],
    ].sort((a, b) => a.timestamp - b.timestamp)
})

describe('sessionRecordingDataLogic', () => {
    let logic: ReturnType<typeof sessionRecordingDataLogic.build>

    beforeEach(async () => {
        useAvailableFeatures([AvailableFeature.RECORDINGS_PERFORMANCE])
        useMocks({
            get: recordingGetDataMocks,
        })
        initKeaTests()
        logic = sessionRecordingDataLogic({ sessionRecordingId: '2' })
        logic.mount()
        jest.spyOn(api, 'get')
    })

    describe('core assumptions', () => {
        it('mounts other logics', async () => {
            await expectLogic(logic).toMount([eventUsageLogic, teamLogic, userLogic])
        })
        it('has default values', async () => {
            await expectLogic(logic).toMatchValues({
                sessionRecordingId: null,
                sessionPlayerData: {
                    bufferedTo: null,
                    metadata: { recordingDurationMs: 0, segments: [], pinnedCount: 0, startAndEndTimesByWindowId: {} },
                    person: null,
                    snapshotsByWindowId: {},
                },
                sessionEventsData: null,
                filters: {},
                chunkPaginationIndex: 0,
                sessionEventsDataLoading: false,
            })
        })
    })

    describe('loading session core', () => {
        it('is triggered by mounting', async () => {
            const expectedData = {
                person: recordingMetaJson.person,
                metadata: parseMetadataResponse(recordingMetaJson),
                bufferedTo: {
                    time: 2725496,
                    windowId: '182830cdf4b28a9-02530f1179ed36-1c525635-384000-182830cdf4c2841',
                },
                next: undefined,
                snapshotsByWindowId: sortedRecordingSnapshotsJson.snapshot_data_by_window_id,
            }
            await expectLogic(logic)
                .toDispatchActions(['loadEntireRecording', 'loadRecordingMetaSuccess', 'loadRecordingSnapshotsSuccess'])
                .toFinishAllListeners()
                .toMatchValues({
                    sessionPlayerData: expectedData,
                })
        })

        it('fetch metadata error', async () => {
            silenceKeaLoadersErrors()
            // Unmount and remount the logic to trigger fetching the data again after the mock change
            logic.unmount()
            useMocks({
                get: {
                    '/api/projects/:team/session_recordings/:id': () => [500, { status: 0 }],
                },
            })
            logic.mount()

            await expectLogic(logic)
                .toDispatchActionsInAnyOrder(['loadRecordingMeta', 'loadRecordingMetaFailure'])
                .toFinishAllListeners()
                .toMatchValues({
                    sessionPlayerData: {
                        bufferedTo: null,
                        metadata: {
                            recordingDurationMs: 0,
                            segments: [],
                            pinnedCount: 0,
                            startAndEndTimesByWindowId: {},
                        },
                        person: null,
                        snapshotsByWindowId: {},
                    },
                })
            resumeKeaLoadersErrors()
        })
        it('fetch metadata success and snapshots error', async () => {
            silenceKeaLoadersErrors()
            // Unmount and remount the logic to trigger fetching the data again after the mock change
            logic.unmount()
            useMocks({
                get: {
                    '/api/projects/:team/session_recordings/:id/snapshots': () => [500, { status: 0 }],
                },
            })
            logic.mount()

            await expectLogic(logic)
                .toDispatchActions(['loadRecordingSnapshots', 'loadRecordingSnapshotsFailure'])
                .toMatchValues({
                    sessionPlayerData: {
                        person: recordingMetaJson.person,
                        metadata: parseMetadataResponse(recordingMetaJson),
                        snapshotsByWindowId: {},
                        bufferedTo: null,
                    },
                })
            resumeKeaLoadersErrors()
        })
    })

    describe('loading session events', () => {
        const expectedEvents = [
            expect.objectContaining(recordingEventsJson[0]),
            expect.objectContaining(recordingEventsJson[1]),
            expect.objectContaining(recordingEventsJson[2]),
            expect.objectContaining(recordingEventsJson[4]),
            expect.objectContaining(recordingEventsJson[5]),
            expect.objectContaining(recordingEventsJson[6]),
        ]

        beforeEach(async () => {
            // Test session events loading in isolation from other features
            useAvailableFeatures([])
            initKeaTests()
            useAvailableFeatures([])
            initKeaTests()
            logic = sessionRecordingDataLogic({ sessionRecordingId: '2' })
            logic.mount()
            api.get.mockClear()
        })

        it('load events after metadata with 1min buffer', async () => {
            await expectLogic(logic, () => {
                logic.actions.loadRecordingMeta()
            })
                .toDispatchActions(['loadRecordingMeta', 'loadRecordingMetaSuccess', 'loadEvents'])
                .toMatchValues({
                    eventsApiParams: {
                        after: '2021-12-09T19:35:59Z',
                        before: '2021-12-09T20:23:24Z',
                        person_id: '1',
                        orderBy: ['timestamp'],
                        properties: {
                            type: 'OR',
                            values: [
                                {
                                    type: 'AND',
                                    values: [
                                        {
                                            key: '$session_id',
                                            operator: 'is_not_set',
                                            type: 'event',
                                            value: 'is_not_set',
                                        },
                                    ],
                                },
                                {
                                    type: 'AND',
                                    values: [
                                        {
                                            key: '$session_id',
                                            operator: 'exact',
                                            type: 'event',
                                            value: ['2'],
                                        },
                                    ],
                                },
                            ],
                        },
                    },
                })
        })
        it('no next url', async () => {
            await expectLogic(logic, () => {
                logic.actions.loadRecordingMeta()
            })
                .toDispatchActions(['loadRecordingMeta', 'loadRecordingMetaSuccess', 'loadEvents', 'loadEventsSuccess'])
                .toNotHaveDispatchedActions(['loadEvents'])
        })
        it('fetch all events and sort by player time', async () => {
            const firstNext = `${EVENTS_SESSION_RECORDING_EVENTS_ENDPOINT}?person_id=1&before=2021-10-28T17:45:12.128000Z&after=2021-10-28T16:45:05Z`
            let count = 0
            useMocks({
                get: {
                    '/api/projects/:team/events': () => [
                        200,
                        { results: recordingEventsJson, next: count++ === 0 ? firstNext : undefined },
                    ],
                },
            })

            await expectLogic(logic, () => {
                logic.actions.loadRecordingMeta()
            })
                .toDispatchActions(['loadRecordingMeta', 'loadRecordingMetaSuccess', 'loadEvents', 'loadEventsSuccess'])
                .toMatchValues({
                    sessionEventsData: {
                        next: firstNext,
                        events: expectedEvents,
                    },
                })
                .toDispatchActions([logic.actionCreators.loadEvents(firstNext), 'loadEventsSuccess'])
                .toNotHaveDispatchedActions(['loadEvents'])

            expect(logic.values.sessionEventsData).toMatchObject({
                next: undefined,
                events: [
                    expect.objectContaining(recordingEventsJson[0]),
                    expect.objectContaining(recordingEventsJson[1]),
                    expect.objectContaining(recordingEventsJson[0]),
                    expect.objectContaining(recordingEventsJson[1]),
                    expect.objectContaining(recordingEventsJson[2]),
                    expect.objectContaining(recordingEventsJson[2]),
                    expect.objectContaining(recordingEventsJson[4]),
                    expect.objectContaining(recordingEventsJson[4]),
                    expect.objectContaining(recordingEventsJson[5]),
                    expect.objectContaining(recordingEventsJson[5]),
                    expect.objectContaining(recordingEventsJson[6]),
                    expect.objectContaining(recordingEventsJson[6]),
                ],
            })

            // data, meta, events, and then first next events
            expect(api.get).toBeCalledTimes(4)
        })
        it('server error mid-fetch', async () => {
            const firstNext = `${EVENTS_SESSION_RECORDING_EVENTS_ENDPOINT}?person_id=1&before=2021-10-28T17:45:12.128000Z&after=2021-10-28T16:45:05Z`
            silenceKeaLoadersErrors()
            api.get
                .mockImplementationOnce(async (url: string) => {
                    if (combineUrl(url).pathname.startsWith(EVENTS_SESSION_RECORDING_META_ENDPOINT)) {
                        return recordingMetaJson
                    }
                })
                .mockImplementationOnce(async (url: string) => {
                    if (combineUrl(url).pathname.match(EVENTS_SESSION_RECORDING_SNAPSHOTS_ENDPOINT_REGEX)) {
                        return { ...recordingSnapshotsJson }
                    }
                })
                .mockImplementationOnce(async (url: string) => {
                    if (combineUrl(url).pathname.startsWith(EVENTS_SESSION_RECORDING_EVENTS_ENDPOINT)) {
                        return { results: recordingEventsJson, next: firstNext }
                    }
                })
                .mockImplementationOnce(async () => {
                    throw new Error('Error in third request')
                })

            await expectLogic(logic, () => {
                logic.actions.loadRecordingMeta()
            })
                .toDispatchActions(['loadRecordingMeta', 'loadRecordingMetaSuccess', 'loadEvents', 'loadEventsSuccess'])
                .toMatchValues({
                    sessionEventsData: {
                        next: firstNext,
                        events: expectedEvents,
                    },
                })
                .toDispatchActions([logic.actionCreators.loadEvents(firstNext), 'loadEventsFailure'])
            resumeKeaLoadersErrors()

            // data, meta, events, and then errored out on first next events
            expect(api.get).toBeCalledTimes(4)
        })
    })

    describe('loading session performance events', () => {
        describe("don't call performance endpoint", () => {
            beforeEach(async () => {
                useAvailableFeatures([])
                initKeaTests()
                logic = sessionRecordingDataLogic({ sessionRecordingId: '2' })
                logic.mount()
                api.get.mockClear()
            })

            it("user doesn't have the performance feature", async () => {
                api.get.mockClear()
                await expectLogic(logic, async () => {
                    logic.actions.loadRecordingMeta()
                })
                    .toDispatchActions(['loadRecordingMeta', 'loadRecordingMetaSuccess'])
                    .toDispatchActionsInAnyOrder([
                        'loadEvents',
                        'loadEventsSuccess',
                        'loadPerformanceEvents',
                        'loadPerformanceEventsSuccess',
                    ])
                    .toMatchValues({
                        performanceEvents: [],
                    })

                // data, meta, events... but not performance events
                expect(api.get).toBeCalledTimes(3)
            })
        })

        it('load performance events', async () => {
            logic = sessionRecordingDataLogic({ sessionRecordingId: '2' })
            logic.mount()

            await expectLogic(logic, () => {
                logic.actions.loadRecordingMeta()
            })
                .toDispatchActions([
                    'loadRecordingMeta',
                    'loadRecordingMetaSuccess',
                    'loadPerformanceEvents',
                    'loadPerformanceEventsSuccess',
                ])
                .toMatchValues({
                    eventsApiParams: {
                        after: '2021-12-09T19:35:59Z',
                        before: '2021-12-09T20:23:24Z',
                        person_id: '1',
                        orderBy: ['timestamp'],
                        properties: {
                            type: 'OR',
                            values: [
                                {
                                    type: 'AND',
                                    values: [
                                        {
                                            key: '$session_id',
                                            operator: 'is_not_set',
                                            type: 'event',
                                            value: 'is_not_set',
                                        },
                                    ],
                                },
                                {
                                    type: 'AND',
                                    values: [
                                        {
                                            key: '$session_id',
                                            operator: 'exact',
                                            type: 'event',
                                            value: ['2'],
                                        },
                                    ],
                                },
                            ],
                        },
                    },
                    performanceEvents: expect.arrayContaining([
                        expect.objectContaining({
                            entry_type: 'navigation',
                        }),
                    ]),
                })
        })
    })

    describe('loading session snapshots', () => {
        it('no next url', async () => {
            await expectLogic(logic, () => {
                logic.actions.loadRecordingSnapshots()
            })
                .toDispatchActions(['loadRecordingSnapshots', 'loadRecordingSnapshotsSuccess'])
                .toMatchValues({
                    sessionPlayerData: {
                        person: recordingMetaJson.person,
                        metadata: parseMetadataResponse(recordingMetaJson),
                        bufferedTo: {
                            time: 2725496,
                            windowId: '182830cdf4b28a9-02530f1179ed36-1c525635-384000-182830cdf4c2841',
                        },
                        next: undefined,
                        snapshotsByWindowId: sortedRecordingSnapshotsJson.snapshot_data_by_window_id,
                    },
                })
                .toNotHaveDispatchedActions(['loadRecordingSnapshots'])
        })

        it('fetch all chunks of recording', async () => {
            const snapshots1 = { snapshot_data_by_window_id: {} }
            const snapshots2 = { snapshot_data_by_window_id: {} }

            Object.keys(sortedRecordingSnapshotsJson.snapshot_data_by_window_id).forEach((windowId) => {
                snapshots1.snapshot_data_by_window_id[windowId] =
                    sortedRecordingSnapshotsJson.snapshot_data_by_window_id[windowId].slice(0, 3)
                snapshots2.snapshot_data_by_window_id[windowId] =
                    sortedRecordingSnapshotsJson.snapshot_data_by_window_id[windowId].slice(3)
            })

            const snapshotUrl = createSnapshotEndpoint(3)
            const firstNext = `${snapshotUrl}/?offset=200&limit=200`
            let nthSnapshotCall = 0
            logic.unmount()
            useAvailableFeatures([])
            useMocks({
                get: {
                    '/api/projects/:team/session_recordings/:id/snapshots': (req) => {
                        if (req.url.pathname.match(EVENTS_SESSION_RECORDING_SNAPSHOTS_ENDPOINT_REGEX)) {
                            const payload = {
                                ...(nthSnapshotCall === 0 ? snapshots1 : snapshots2),
                                next: nthSnapshotCall === 0 ? firstNext : undefined,
                            }
                            nthSnapshotCall += 1
                            return [200, payload]
                        }
                    },
                },
            })

            logic.mount()

            await expectLogic(preflightLogic).toDispatchActions(['loadPreflightSuccess'])
            api.get.mockClear()
            await expectLogic(logic).toMount([eventUsageLogic]).toFinishAllListeners()

            await expectLogic(logic)
                .toDispatchActions(['loadRecordingSnapshots', 'loadRecordingSnapshotsSuccess'])
                .toMatchValues({
                    sessionPlayerData: {
                        person: recordingMetaJson.person,
                        metadata: parseMetadataResponse(recordingMetaJson),
                        bufferedTo: {
                            time: 167777,
                            windowId: '182830cdf4b28a9-02530f1179ed36-1c525635-384000-182830cdf4c2841',
                        },
                        snapshotsByWindowId: snapshots1.snapshot_data_by_window_id,
                        next: firstNext,
                    },
                })

            await expectLogic(logic)
                .toDispatchActions([
                    logic.actionCreators.loadRecordingSnapshots(firstNext),
                    'loadRecordingSnapshotsSuccess',
                ])
                .toMatchValues({
                    sessionPlayerData: {
                        person: recordingMetaJson.person,
                        metadata: parseMetadataResponse(recordingMetaJson),
                        bufferedTo: {
                            time: 2725496,
                            windowId: '182830cdf4b28a9-02530f1179ed36-1c525635-384000-182830cdf4c2841',
                        },
                        snapshotsByWindowId: sortedRecordingSnapshotsJson.snapshot_data_by_window_id,
                        next: undefined,
                    },
                })
                .toFinishAllListeners()
            expect(api.get).toBeCalledTimes(4) // 2 calls to loadRecordingSnapshots + 1 call to loadRecordingMeta + 1 call to loadPerformanceEvents
        })

        it('server error mid-way through recording', async () => {
            let nthSnapshotCall = 0
            logic.unmount()
            useAvailableFeatures([])
            useMocks({
                get: {
                    '/api/projects/:team/session_recordings/:id/snapshots': (req) => {
                        if (req.url.pathname.match(EVENTS_SESSION_RECORDING_SNAPSHOTS_ENDPOINT_REGEX)) {
                            if (nthSnapshotCall === 0) {
                                const payload = {
                                    ...recordingSnapshotsJson,
                                    next: firstNext,
                                }
                                nthSnapshotCall += 1
                                return [200, payload]
                            } else {
                                throw new Error('Error in second request')
                            }
                        }
                    },
                },
            })
            logic.mount()

            await expectLogic(preflightLogic).toDispatchActions(['loadPreflightSuccess'])
            await expectLogic(logic).toMount([eventUsageLogic]).toFinishAllListeners()
            api.get.mockClear()

            const snapshotUrl = createSnapshotEndpoint(1)
            const firstNext = `${snapshotUrl}/?offset=200&limit=200`
            silenceKeaLoadersErrors()

            await expectLogic(logic, async () => {
                await logic.actions.loadRecordingSnapshots()
            }).toDispatchActions(['loadRecordingSnapshots', 'loadRecordingSnapshotsSuccess'])

            expectLogic(logic).toMatchValues({
                sessionPlayerData: {
                    person: recordingMetaJson.person,
                    metadata: parseMetadataResponse(recordingMetaJson),
                    bufferedTo: {
                        time: 2725496,
                        windowId: '182830cdf4b28a9-02530f1179ed36-1c525635-384000-182830cdf4c2841',
                    },
                    snapshotsByWindowId: sortedRecordingSnapshotsJson.snapshot_data_by_window_id,
                    next: firstNext,
                },
            })
            await expectLogic(logic)
                .toDispatchActions([
                    logic.actionCreators.loadRecordingSnapshots(firstNext),
                    'loadRecordingSnapshotsFailure',
                ])
                .toFinishAllListeners()
            resumeKeaLoadersErrors()
            expect(api.get).toBeCalledTimes(2)
        })
    })

    describe('report usage', () => {
        it('send `recording loaded` event only when entire recording has loaded', async () => {
            await expectLogic(logic, () => {
                logic.actions.loadEntireRecording()
            })
                .toDispatchActions(['loadEntireRecording'])
                .toDispatchActionsInAnyOrder([
                    'loadRecordingMeta',
                    'loadRecordingMetaSuccess',
                    'loadRecordingSnapshots',
                    'loadRecordingSnapshotsSuccess',
                    'loadEvents',
                    'loadEventsSuccess',
                    'loadPerformanceEvents',
                    'loadPerformanceEventsSuccess',
                ])
                .toDispatchActions([eventUsageLogic.actionTypes.reportRecording]) // only dispatch once
                .toNotHaveDispatchedActions([
                    eventUsageLogic.actionTypes.reportRecording,
                    eventUsageLogic.actionTypes.reportRecording,
                ])
        })
        it('send `recording viewed` and `recording analyzed` event on first contentful paint', async () => {
            await expectLogic(logic, () => {
                logic.actions.loadEntireRecording()
            })
                .toDispatchActions(['loadEntireRecording', 'loadRecordingSnapshotsSuccess'])
                .toDispatchActionsInAnyOrder([
                    eventUsageLogic.actionTypes.reportRecording, // loaded
                    eventUsageLogic.actionTypes.reportRecording, // viewed
                    eventUsageLogic.actionTypes.reportRecording, // analyzed
                ])
                .toMatchValues({
                    chunkPaginationIndex: 1,
                })
        })
    })
})
