# service.ts 移植任务分工（必须严格遵守）

上游文件：`.hdsi_reference/src/service.ts`（8524 行，HDS-Interlude 1.0.1-beta6-rebuild）。
本文件把 InterludeService 按职责拆成 mixin 模块；所有 mixin 由 `hdsi/service.py` 组合：

```python
class InterludeService(ServiceBaseMixin, ServiceStoryMixin, ServiceInboundMixin,
                       ServiceMediaMixin, ServiceNarrativeMixin, ServiceDecisionMixin,
                       ServiceScheduleMixin, ServiceMemoryMixin):
    pass
```

## 共享约定（所有 mixin 必须遵守）

- 同步方法；上游 async 去掉。跨 mixin 调用用 snake_case 方法名（见下表）。
- 共享状态挂在 self 上，字段名沿用上游语义，例如：
  self.config（dict）、self.ctx（RuntimeContext）、self.platform（PlatformAdapter）、
  self.narrator、self.compactor、self.embedder、self.sticker_describer、self.vision_describer、
  self.model_routing、self.queues、self.buffered_narrative_turns、self.buffered_group_turns、
  self.scheduled_compactions、self.scheduled_alter_analyses、self.narrating_stories、
  self.group_willingness、self.due_intent_wake_timers、self.history_vectors 等，照上游字段抄。
- 数据库：self.db_get(table, query, options=None) / self.db_create(table, data) /
  self.db_set(table, query, data) / self.db_remove(table, query) / self.purge_table(table, query, fallback)
  （由 ServiceBaseMixin 提供，内部走 hdsi/store.py）。时间字段读出来是 datetime。
- 日志：self.report_operation(verbosity, level, story, phase, message, *args)、
  self.report(level, story, phase, message, *args)、self.report_standalone(level, message, *args)、
  self.report_standalone_operation(verbosity, level, message, *args)、self.allows_verbosity(x)、
  self.emit_log(...)（由 ServiceBaseMixin 提供）。格式串 %s/%d 保留。
- 定时器：上游 ctx.setTimeout(fn, ms) 到 self.ctx.set_timeout(fn, milliseconds) 返回 handle；
  ctx.setInterval 到 self.ctx.set_interval；清理用 self.ctx.clear_timer(handle)。
- 平台调用映射：session.bot.sendMessage(channelId, content) 到 self.platform.send_group/send_private（按上下文）；
  bot.getImage(url)/ctx.http.get 到 self.platform.fetch_image/fetch_audio/http_get；
  bot.getGuildMember(g,u) 到 self.platform.group_member_name(g,u)；ctx.bots 到 self.platform.list_bots()；
  ctx.puppeteer 到 self.platform.render_page/search_web/downscale_image（None 走上游降级分支）；
  node:fs/promises 到 self.platform.read_file/list_files/file_size；node:path 用 os.path。
- 时间：new Date() 到 now_utc()；Date 运算用 timedelta；Time.hour/minute 用 timedelta(hours=...)。
- JSON 数据是 dict；可选字段用 .get()；{...a, ...b} 到 {**a, **b}；?. 改成显式 None 判断。
- 不要改上游 prompt/日志/文案（中文和英文都逐字保留）；不要优化逻辑。
- 每个 mixin 文件头注明"上游 src/service.ts 行号区间"。
- 上游返回值是 Promise 的直接返回结果；上游 void 返回 None。

## 方法名对照表（用同名 snake_case，不要另起名字）

| 上游方法 | Python 方法 | 行号 |
| --- | --- | --- |
| `constructor` | `constructor` | 717 |
| `startBackgroundTasks` | `start_background_tasks` | 741 |
| `setNarrator` | `set_narrator` | 758 |
| `getNarrator` | `get_narrator` | 759 |
| `setCompactor` | `set_compactor` | 760 |
| `setEmbedder` | `set_embedder` | 762 |
| `setDesktopEventSink` | `set_desktop_event_sink` | 765 |
| `setDesktopDeliveryHandler` | `set_desktop_delivery_handler` | 767 |
| `getDesktopRuntimePhase` | `get_desktop_runtime_phase` | 768 |
| `setDesktopRuntimePhase` | `set_desktop_runtime_phase` | 769 |
| `desktopRuntimeSnapshot` | `desktop_runtime_snapshot` | 800 |
| `desktopTimelineSnapshot` | `desktop_timeline_snapshot` | 809 |
| `desktopPurgeRange` | `desktop_purge_range` | 846 |
| `desktopTimelineRange` | `desktop_timeline_range` | 864 |
| `setDesktopCursorAt` | `set_desktop_cursor_at` | 911 |
| `receiveDesktopEvent` | `receive_desktop_event` | 919 |
| `canHandleSession` | `can_handle_session` | 932 |
| `canHandleGroupSession` | `can_handle_group_session` | 955 |
| `groupRule` | `group_rule` | 967 |
| `canHandleParticipant` | `can_handle_participant` | 973 |
| `canManageSession` | `can_manage_session` | 981 |
| `canHandleStory` | `can_handle_story` | 991 |
| `findStory` | `find_story` | 998 |
| `getPausedStory` | `get_paused_story` | 1033 |
| `getCanonicalStory` | `get_canonical_story` | 1046 |
| `findParticipant` | `find_participant` | 1063 |
| `participants` | `participants` | 1075 |
| `createStory` | `create_story` | 1082 |
| `storyStartReadiness` | `story_start_readiness` | 1122 |
| `ensureParticipant` | `ensure_participant` | 1158 |
| `updateSetting` | `update_setting` | 1222 |
| `setStatus` | `set_status` | 1229 |
| `recentEntries` | `recent_entries` | 1235 |
| `recentEntriesForPrompt` | `recent_entries_for_prompt` | 1248 |
| `memories` | `memories` | 1264 |
| `adminFacts` | `admin_facts` | 1277 |
| `adminPendingIntents` | `admin_pending_intents` | 1284 |
| `adminStatePatches` | `admin_state_patches` | 1291 |
| `addAdminScriptNote` | `add_admin_script_note` | 1299 |
| `addAdminFact` | `add_admin_fact` | 1312 |
| `forgetAdminFact` | `forget_admin_fact` | 1325 |
| `cancelAdminIntent` | `cancel_admin_intent` | 1332 |
| `rejectAdminStatePatch` | `reject_admin_state_patch` | 1339 |
| `clearSettingOverlay` | `clear_setting_overlay` | 1347 |
| `rebaseTimeline` | `rebase_timeline` | 1355 |
| `clearSettingOverlayUnlocked` | `clear_setting_overlay_unlocked` | 1383 |
| `purgeAllStoryData` | `purge_all_story_data` | 1431 |
| `purgeAllData` | `purge_all_data` | 1456 |
| `purgePlatformData` | `purge_platform_data` | 1471 |
| `clearDatabase` | `clear_database` | 1486 |
| `purgeStoryRange` | `purge_story_range` | 1543 |
| `receiveGroup` | `receive_group` | 1605 |
| `receive` | `receive` | 1645 |
| `groupSenderName` | `group_sender_name` | 1720 |
| `lookupGroupMemberName` | `lookup_group_member_name` | 1735 |
| `bufferGroupMessage` | `buffer_group_message` | 1750 |
| `flushGroupTurn` | `flush_group_turn` | 1769 |
| `groupMessages` | `group_messages` | 1929 |
| `groupCooldownActive` | `group_cooldown_active` | 1953 |
| `groupChatCapabilities` | `group_chat_capabilities` | 1963 |
| `privateChatCapabilities` | `private_chat_capabilities` | 1977 |
| `executeGroupReactions` | `execute_group_reactions` | 1985 |
| `resolveSticker` | `resolve_sticker` | 2035 |
| `resolveNativeFace` | `resolve_native_face` | 2045 |
| `sendSticker` | `send_sticker` | 2059 |
| `sendNativeFace` | `send_native_face` | 2107 |
| `sendGroupMessage` | `send_group_message` | 2147 |
| `bufferUserNarrative` | `buffer_user_narrative` | 2189 |
| `signalIncomingInterruption` | `signal_incoming_interruption` | 2209 |
| `deliverEarlyPrivateReply` | `deliver_early_private_reply` | 2221 |
| `describeUserEvent` | `describe_user_event` | 2276 |
| `scanStickerLibrary` | `scan_sticker_library` | 2303 |
| `refreshStickerCatalog` | `refresh_sticker_catalog` | 2386 |
| `semanticStickerEmbeddingEnabled` | `semantic_sticker_embedding_enabled` | 2392 |
| `backfillStickerEmbeddings` | `backfill_sticker_embeddings` | 2398 |
| `stickerCatalogForSession` | `sticker_catalog_for_session` | 2410 |
| `rankStickerAssets` | `rank_sticker_assets` | 2422 |
| `semanticTurnEmbeddingEnabled` | `semantic_turn_embedding_enabled` | 2429 |
| `previousSceneSummaries` | `previous_scene_summaries` | 2440 |
| `pruneWorkingDetails` | `prune_working_details` | 2457 |
| `recallHistory` | `recall_history` | 2466 |
| `ensureHistoryVectors` | `ensure_history_vectors` | 2539 |
| `invalidateHistoryVectors` | `invalidate_history_vectors` | 2573 |
| `backfillHistoryEmbeddings` | `backfill_history_embeddings` | 2590 |
| `loadNativeAudio` | `load_native_audio` | 2651 |
| `fetchNativeAudio` | `fetch_native_audio` | 2666 |
| `describeVisionEvent` | `describe_vision_event` | 2719 |
| `loadNativeImages` | `load_native_images` | 2733 |
| `describeCurrentImages` | `describe_current_images` | 2749 |
| `fetchNativeImage` | `fetch_native_image` | 2769 |
| `imageBytesToNative` | `image_bytes_to_native` | 2817 |
| `downscaleImageForVision` | `downscale_image_for_vision` | 2834 |
| `renderAnimatedImageFrame` | `render_animated_image_frame` | 2868 |
| `invalidateBufferedNarratives` | `invalidate_buffered_narratives` | 2898 |
| `hasPendingNarrative` | `has_pending_narrative` | 2926 |
| `flushBufferedNarrative` | `flush_buffered_narrative` | 2937 |
| `advanceStory` | `advance_story` | 3140 |
| `deliverMessages` | `deliver_messages` | 3150 |
| `compactStory` | `compact_story` | 3157 |
| `compactOverlay` | `compact_overlay` | 3165 |
| `adminOverlayStatus` | `admin_overlay_status` | 3171 |
| `sweep` | `sweep` | 3188 |
| `advanceUnlocked` | `advance_unlocked` | 3217 |
| `decide` | `decide` | 3437 |
| `shouldRefreshContinuity` | `should_refresh_continuity` | 3604 |
| `planAutomaticTimeline` | `plan_automatic_timeline` | 3613 |
| `isTimelineDirectorFused` | `is_timeline_director_fused` | 3689 |
| `persistTimelineRetry` | `persist_timeline_retry` | 3698 |
| `tryDecide` | `try_decide` | 3720 |
| `persistDecision` | `persist_decision` | 3833 |
| `persistTimelineSceneAnchor` | `persist_timeline_scene_anchor` | 4186 |
| `adminSchedulePreplan` | `admin_schedule_preplan` | 4197 |
| `requestSchedulePreplanRebuild` | `request_schedule_preplan_rebuild` | 4201 |
| `emotionalOffsetForPrompt` | `emotional_offset_for_prompt` | 4227 |
| `updateAlterSystem` | `update_alter_system` | 4231 |
| `scheduleAlterAnalysis` | `schedule_alter_analysis` | 4248 |
| `analyzeAlterSystem` | `analyze_alter_system` | 4259 |
| `appendEntry` | `append_entry` | 4312 |
| `appendMemory` | `append_memory` | 4336 |
| `contactThreads` | `contact_threads` | 4350 |
| `facts` | `facts` | 4393 |
| `webObservations` | `web_observations` | 4449 |
| `activeScene` | `active_scene` | 4468 |
| `activeArc` | `active_arc` | 4476 |
| `appendIntent` | `append_intent` | 4484 |
| `activeConsequencesAndExpire` | `active_consequences_and_expire` | 4512 |
| `applyIntentUpdates` | `apply_intent_updates` | 4537 |
| `appendBrowserIntent` | `append_browser_intent` | 4562 |
| `executeDeferredBrowserIntent` | `execute_deferred_browser_intent` | 4593 |
| `collectWebObservation` | `collect_web_observation` | 4603 |
| `saveWebObservation` | `save_web_observation` | 4672 |
| `persistCollectedWebObservation` | `persist_collected_web_observation` | 4691 |
| `findCachedWebObservation` | `find_cached_web_observation` | 4699 |
| `scheduleNarrativeRetry` | `schedule_narrative_retry` | 4724 |
| `dueIntents` | `due_intents` | 4746 |
| `upcomingNarrativeIntents` | `upcoming_narrative_intents` | 4759 |
| `scheduleDueIntentWake` | `schedule_due_intent_wake` | 4769 |
| `scheduleNextSplitWake` | `schedule_next_split_wake` | 4805 |
| `deliverDueSplitSegments` | `deliver_due_split_segments` | 4814 |
| `pendingFollowUpCommitments` | `pending_follow_up_commitments` | 4893 |
| `appendFollowUpCommitment` | `append_follow_up_commitment` | 4899 |
| `applyFollowUpResolutions` | `apply_follow_up_resolutions` | 4940 |
| `deferUnresolvedDueFollowUps` | `defer_unresolved_due_follow_ups` | 4978 |
| `appendProactiveCheck` | `append_proactive_check` | 5002 |
| `cancelPendingOutgoingMessages` | `cancel_pending_outgoing_messages` | 5046 |
| `sendScheduledMessages` | `send_scheduled_messages` | 5095 |
| `sendOutgoingMessages` | `send_outgoing_messages` | 5107 |
| `confirmOutgoingDeliveries` | `confirm_outgoing_deliveries` | 5193 |
| `recordOutgoingDeliveryFailure` | `record_outgoing_delivery_failure` | 5233 |
| `updateScriptDeliveryOutcome` | `update_script_delivery_outcome` | 5253 |
| `recordPlatformDeliveryOutcome` | `record_platform_delivery_outcome` | 5300 |
| `resolveLiteralQuoteMessageId` | `resolve_literal_quote_message_id` | 5313 |
| `recordAutomaticDelivery` | `record_automatic_delivery` | 5325 |
| `splitOutgoingMessage` | `split_outgoing_message` | 5372 |
| `typingDelayMilliseconds` | `typing_delay_milliseconds` | 5379 |
| `findBotForParticipant` | `find_bot_for_participant` | 5389 |
| `scheduleUrgeAdvance` | `schedule_urge_advance` | 5419 |
| `isAutomaticAdvancePaused` | `is_automatic_advance_paused` | 5442 |
| `dueConversationFollowUps` | `due_conversation_follow_ups` | 5447 |
| `completeConversationFollowUps` | `complete_conversation_follow_ups` | 5459 |
| `isAutomaticAdvanceDue` | `is_automatic_advance_due` | 5475 |
| `pauseAutomaticAdvanceAfterUserMessage` | `pause_automatic_advance_after_user_message` | 5485 |
| `pauseAutomaticAdvanceAfterDelayedReply` | `pause_automatic_advance_after_delayed_reply` | 5505 |
| `scheduleConversationFollowUpsAfterTurn` | `schedule_conversation_follow_ups_after_turn` | 5511 |
| `scheduleNextAutomaticAdvance` | `schedule_next_automatic_advance` | 5543 |
| `schedulePreplanAnchoredTime` | `schedule_preplan_anchored_time` | 5563 |
| `mainModelLabel` | `main_model_label` | 5589 |
| `participantPreset` | `participant_preset` | 5597 |
| `initialStorySetting` | `initial_story_setting` | 5603 |
| `resetParticipantCanon` | `reset_participant_canon` | 5621 |
| `userAccountRule` | `user_account_rule` | 5637 |
| `getParticipant` | `get_participant` | 5643 |
| `recordIncomingMessage` | `record_incoming_message` | 5647 |
| `markParticipantSeen` | `mark_participant_seen` | 5659 |
| `recordCharacterMessage` | `record_character_message` | 5666 |
| `updateParticipantState` | `update_participant_state` | 5676 |
| `migrateLegacyStory` | `migrate_legacy_story` | 5683 |
| `migrateLegacyBranchIntoShared` | `migrate_legacy_branch_into_shared` | 5736 |
| `ensureContinuity` | `ensure_continuity` | 5840 |
| `compactionFingerprint` | `compaction_fingerprint` | 5866 |
| `compactionIsBackedOff` | `compaction_is_backed_off` | 5872 |
| `noteCompactionFailure` | `note_compaction_failure` | 5881 |
| `compactionCheckpointAdvanced` | `compaction_checkpoint_advanced` | 5891 |
| `scheduleCompaction` | `schedule_compaction` | 5898 |
| `compactStories` | `compact_stories` | 5986 |
| `getSchedulePreplan` | `get_schedule_preplan` | 6003 |
| `schedulePreplanEvidence` | `schedule_preplan_evidence` | 6008 |
| `saveSchedulePreplan` | `save_schedule_preplan` | 6034 |
| `prepareSchedulePreplanReview` | `prepare_schedule_preplan_review` | 6047 |
| `requestSchedulePreplan` | `request_schedule_preplan` | 6083 |
| `persistSchedulePreplanReview` | `persist_schedule_preplan_review` | 6092 |
| `scheduleStreamScriptRecovery` | `schedule_stream_script_recovery` | 6131 |
| `persistStreamScriptRecovery` | `persist_stream_script_recovery` | 6148 |
| `compactUnlocked` | `compact_unlocked` | 6163 |
| `prepareCompaction` | `prepare_compaction` | 6199 |
| `applyCompaction` | `apply_compaction` | 6263 |
| `compactOverlayUnlocked` | `compact_overlay_unlocked` | 6273 |
| `overlaySnapshotsForPrompt` | `overlay_snapshots_for_prompt` | 6329 |
| `rebuildLiveOverlayState` | `rebuild_live_overlay_state` | 6348 |
| `persistCompaction` | `persist_compaction` | 6394 |
| `persistFact` | `persist_fact` | 6535 |
| `embedText` | `embed_text` | 6582 |
| `scheduleFactEmbeddingBackfill` | `schedule_fact_embedding_backfill` | 6592 |
| `backfillFactEmbeddings` | `backfill_fact_embeddings` | 6605 |
| `persistStatePatch` | `persist_state_patch` | 6617 |
| `developmentForPrompt` | `development_for_prompt` | 6714 |
| `report` | `report` | 6724 |
| `reportOperation` | `report_operation` | 6731 |
| `writeReport` | `write_report` | 6736 |
| `reportStandalone` | `report_standalone` | 6757 |
| `reportTokenUsage` | `report_token_usage` | 6763 |
| `reportStandaloneOperation` | `report_standalone_operation` | 6770 |
| `writeStandalone` | `write_standalone` | 6775 |
| `resolveCompactionFacts` | `resolve_compaction_facts` | 6792 |
| `markContinuityDirty` | `mark_continuity_dirty` | 6804 |
| `emitLog` | `emit_log` | 6811 |
| `reportBlindModeHealth` | `report_blind_mode_health` | 6818 |
| `allowsVerbosity` | `allows_verbosity` | 6827 |
| `getStory` | `get_story` | 6833 |
| `dbGet` | `db_get` | 6893 |
| `repairCanonicalOneBotStoryTransport` | `repair_canonical_one_bot_story_transport` | 6903 |
| `dbCreate` | `db_create` | 6939 |
| `findPossiblyCommittedCreate` | `find_possibly_committed_create` | 6956 |
| `dbSet` | `db_set` | 6986 |
| `dbRemove` | `db_remove` | 6990 |
| `purgeTable` | `purge_table` | 6999 |

## 顶层导出函数对照表

| 上游导出 | Python 名称 | 行号 |
| --- | --- | --- |
| `extractSessionVoiceCount` | `extract_session_voice_count` | 7084 |
| `extractSessionAudioSources` | `extract_session_audio_sources` | 7102 |
| `extractSessionFileFacts` | `extract_session_file_facts` | 7158 |
| `describeGroupAttachments` | `describe_group_attachments` | 7188 |
| `guessAudioFormat` | `guess_audio_format` | 7229 |
| `calibratedNativeFaceWillingness` | `calibrated_native_face_willingness` | 7282 |
| `stableStickerAssetId` | `stable_sticker_asset_id` | 7337 |
| `extractUserReportedTimes` | `extract_user_reported_times` | 7351 |
| `describeQuotedMessage` | `describe_quoted_message` | 7399 |
| `normalizeQuotedMessageContent` | `normalize_quoted_message_content` | 7417 |
| `normalizeAllowedReactions` | `normalize_allowed_reactions` | 7446 |
| `normalizeTimelinePlan` | `normalize_timeline_plan` | 7483 |
| `describeTimelinePlanRejection` | `describe_timeline_plan_rejection` | 7503 |
| `timelineEntryPromptProjection` | `timeline_entry_prompt_projection` | 7521 |
| `normalizeGroupChatActions` | `normalize_group_chat_actions` | 7531 |
| `formatGroupSpeaker` | `format_group_speaker` | 7564 |
| `normalizeGroupVisibleReply` | `normalize_group_visible_reply` | 7585 |
| `visibleReplyMode` | `visible_reply_mode` | 7594 |
| `hasRequiredNarrativeScript` | `has_required_narrative_script` | 7711 |
| `resolveBlindModeConfig` | `resolve_blind_mode_config` | 7715 |
| `resolveBlackBoxConfig` | `resolve_black_box_config` | 7723 |
| `normalizeScenePresenceDrafts` | `normalize_scene_presence_drafts` | 7807 |
| `normalizeInteraction` | `normalize_interaction` | 7968 |
| `groupDueIntents` | `group_due_intents` | 8113 |
| `shouldSupersedeNarrativeRequest` | `should_supersede_narrative_request` | 8133 |
| `normalizeDatabaseRow` | `normalize_database_row` | 8169 |
| `historyLexicalScore` | `history_lexical_score` | 8247 |
| `SEMANTIC_STICKER_LIMIT` | `s_e_m_a_n_t_i_c__s_t_i_c_k_e_r__l_i_m_i_t` | 8284 |
| `rankStickerCatalog` | `rank_sticker_catalog` | 8289 |
| `shouldDownscaleImage` | `should_downscale_image` | 8300 |
| `detectLiveScriptTimeOverflow` | `detect_live_script_time_overflow` | 8417 |

## 各 mixin 负责的行号区间

| mixin 文件 | 上游行号 | 说明 |
| --- | --- | --- |
| hdsi/service_base.py | 624-760, 2240-2270, 4211-4230, 5395-5420, 5570-5590, 5755-5830, 6724-7010 | 字段/构造/定时器/配置 getter/serial 队列/日志/数据库封装 |
| hdsi/service_story.py | 932-1221, 1222-1605, 5589-5700, 5683-5866 | 会话过滤/故事与参与者 CRUD/设定/管理命令/purge/迁移/continuity |
| hdsi/service_inbound.py | 1605-1953 | receive/receiveGroup/群消息缓冲与 flush/群成员名 |
| hdsi/service_media.py | 1953-3140 | 能力/reaction/贴纸/媒体/图片/音频/early reply/缓冲叙事 flush/史官检索向量 |
| hdsi/service_narrative.py | 3140-3833 | advance/sweep/decide/tryDecide/timeline director |
| hdsi/service_decision.py | 3833-4393 | persistDecision/alter/appendEntry/appendMemory/contactThreads |
| hdsi/service_schedule.py | 4393-5600 | facts/web/intent/follow-up/delivery/split/wake/自动推进调度 |
| hdsi/service_memory.py | 5600-6720 | continuity/compaction/schedule preplan/embedding/state patch/development |
| hdsi/service_types.py | 65-620 | Config 及各子配置接口（TypedDict；只做类型，不搬运逻辑） |
| hdsi/service_helpers.py | 7084-8524 | 所有顶层导出纯函数 |
