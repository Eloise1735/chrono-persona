package com.kaltsit.msmobile

import android.app.Application
import androidx.compose.runtime.getValue
import androidx.compose.runtime.mutableStateOf
import androidx.compose.runtime.setValue
import androidx.lifecycle.AndroidViewModel
import androidx.lifecycle.viewModelScope
import com.google.gson.JsonParser
import com.kaltsit.msmobile.data.AppRepository
import com.kaltsit.msmobile.data.AutomationRunItem
import com.kaltsit.msmobile.data.CreateEventRequest
import com.kaltsit.msmobile.data.CreateKeyRecordRequest
import com.kaltsit.msmobile.data.EventAnchor
import com.kaltsit.msmobile.data.KeyRecord
import com.kaltsit.msmobile.data.Snapshot
import com.kaltsit.msmobile.data.UpdateEventRequest
import com.kaltsit.msmobile.data.UpdateKeyRecordRequest
import kotlinx.coroutines.launch
import java.time.LocalDate

enum class AppTab { DASHBOARD, HISTORY, KEY_RECORDS, SETTINGS }
enum class HistoryTab { SNAPSHOTS, EVENTS }

data class EventDraft(
    val id: Int? = null,
    val date: String = LocalDate.now().toString(),
    val title: String = "",
    val description: String = "",
    val keywordsCsv: String = "",
    val categoriesCsv: String = "",
)

data class KeyRecordDraft(
    val id: Int? = null,
    val type: String = "important_item",
    val title: String = "",
    val contentText: String = "",
    val tagsCsv: String = "",
    val startDate: String = "",
    val endDate: String = "",
    val status: String = "active",
    val source: String = "manual",
)

class MainViewModel(application: Application) : AndroidViewModel(application) {
    private val repository = AppRepository(application.applicationContext)

    private val keyRecordTypeAllowed = setOf(
        "important_date",
        "important_item",
        "key_collaboration",
        "medical_advice",
    )

    private val keyRecordStatusAllowed = setOf("active", "archived")
    private val keyRecordSourceAllowed = setOf("manual", "conversation", "generated")

    var selectedTab by mutableStateOf(AppTab.DASHBOARD)
        private set

    var statusMessage by mutableStateOf("就绪")
        private set

    var isBusy by mutableStateOf(false)
        private set

    var latestSnapshot by mutableStateOf<Snapshot?>(null)
        private set

    var latestAutomation by mutableStateOf<AutomationRunItem?>(null)
        private set

    var historyTab by mutableStateOf(HistoryTab.SNAPSHOTS)
        private set

    var snapshots by mutableStateOf(emptyList<Snapshot>())
        private set

    var events by mutableStateOf(emptyList<EventAnchor>())
        private set

    var historySearchQuery by mutableStateOf("")
        private set

    var includeArchivedEvents by mutableStateOf(false)
        private set

    var showEventDialog by mutableStateOf(false)
        private set

    var eventDraft by mutableStateOf(EventDraft())
        private set

    var showCreateSnapshotDialog by mutableStateOf(false)
        private set

    var snapshotDraft by mutableStateOf("")
        private set

    var keyRecords by mutableStateOf(emptyList<KeyRecord>())
        private set

    var keyRecordSearchQuery by mutableStateOf("")
        private set

    var showKeyRecordDialog by mutableStateOf(false)
        private set

    var keyRecordDraft by mutableStateOf(KeyRecordDraft())
        private set

    var baseUrlInput by mutableStateOf(repository.getBaseUrl())
        private set

    var savedBaseUrl by mutableStateOf(repository.getBaseUrl())
        private set

    init {
        refreshAll()
    }

    fun selectTab(tab: AppTab) {
        selectedTab = tab
    }

    fun switchHistoryTab(tab: HistoryTab) {
        historyTab = tab
        if (historySearchQuery.isBlank()) {
            refreshHistory()
        }
    }

    fun onHistorySearchChange(value: String) {
        historySearchQuery = value
    }

    fun onKeyRecordSearchChange(value: String) {
        keyRecordSearchQuery = value
    }

    fun onBaseUrlInputChange(value: String) {
        baseUrlInput = value
    }

    fun onSnapshotDraftChange(value: String) {
        snapshotDraft = value
    }

    fun toggleCreateSnapshotDialog(show: Boolean) {
        showCreateSnapshotDialog = show
        if (!show) snapshotDraft = ""
    }

    fun onIncludeArchivedChange(value: Boolean) {
        includeArchivedEvents = value
        if (historyTab == HistoryTab.EVENTS) refreshHistory()
    }

    fun openCreateEventDialog() {
        eventDraft = EventDraft()
        showEventDialog = true
    }

    fun openEditEventDialog(event: EventAnchor) {
        eventDraft = EventDraft(
            id = event.id,
            date = event.date,
            title = event.title,
            description = event.description,
            keywordsCsv = parseJsonArrayToCsv(event.triggerKeywords),
            categoriesCsv = parseJsonArrayToCsv(event.categories),
        )
        showEventDialog = true
    }

    fun closeEventDialog() {
        showEventDialog = false
    }

    fun updateEventDraft(
        date: String = eventDraft.date,
        title: String = eventDraft.title,
        description: String = eventDraft.description,
        keywordsCsv: String = eventDraft.keywordsCsv,
        categoriesCsv: String = eventDraft.categoriesCsv,
    ) {
        eventDraft = eventDraft.copy(
            date = date,
            title = title,
            description = description,
            keywordsCsv = keywordsCsv,
            categoriesCsv = categoriesCsv,
        )
    }

    fun openCreateKeyRecordDialog() {
        keyRecordDraft = KeyRecordDraft()
        showKeyRecordDialog = true
    }

    fun openEditKeyRecordDialog(record: KeyRecord) {
        keyRecordDraft = KeyRecordDraft(
            id = record.id,
            type = record.type,
            title = record.title,
            contentText = record.contentText,
            tagsCsv = parseJsonArrayToCsv(record.tags),
            startDate = record.startDate.orEmpty(),
            endDate = record.endDate.orEmpty(),
            status = record.status,
            source = record.source,
        )
        showKeyRecordDialog = true
    }

    fun closeKeyRecordDialog() {
        showKeyRecordDialog = false
    }

    fun updateKeyRecordDraft(
        type: String = keyRecordDraft.type,
        title: String = keyRecordDraft.title,
        contentText: String = keyRecordDraft.contentText,
        tagsCsv: String = keyRecordDraft.tagsCsv,
        startDate: String = keyRecordDraft.startDate,
        endDate: String = keyRecordDraft.endDate,
        status: String = keyRecordDraft.status,
        source: String = keyRecordDraft.source,
    ) {
        keyRecordDraft = keyRecordDraft.copy(
            type = type,
            title = title,
            contentText = contentText,
            tagsCsv = tagsCsv,
            startDate = startDate,
            endDate = endDate,
            status = status,
            source = source,
        )
    }

    fun saveBaseUrl() {
        repository.setBaseUrl(baseUrlInput)
        savedBaseUrl = repository.getBaseUrl()
        statusMessage = "后端地址已保存：$savedBaseUrl"
    }

    fun testConnection() {
        launchTask("连接测试通过") {
            val (snapshot, _) = repository.fetchDashboard()
            latestSnapshot = snapshot
        }
    }

    fun refreshAll() {
        refreshDashboard()
        refreshHistory()
        refreshKeyRecords()
    }

    fun refreshDashboard() {
        launchTask("仪表盘已刷新") {
            val (snapshot, automation) = repository.fetchDashboard()
            latestSnapshot = snapshot
            latestAutomation = automation
        }
    }

    fun refreshHistory() {
        launchTask("历史列表已刷新") {
            if (historyTab == HistoryTab.SNAPSHOTS) {
                snapshots = repository.listSnapshots(limit = 50)
            } else {
                events = repository.listEvents(limit = 50, includeArchived = includeArchivedEvents)
            }
        }
    }

    fun searchHistory() {
        val query = historySearchQuery.trim()
        if (query.isBlank()) {
            refreshHistory()
            return
        }
        launchTask("检索完成") {
            val (snapshotResult, eventResult) = repository.searchHistory(query)
            if (historyTab == HistoryTab.SNAPSHOTS) {
                snapshots = snapshotResult
            } else {
                events = if (includeArchivedEvents) eventResult else eventResult.filter { it.archived == 0 }
            }
        }
    }

    fun submitSnapshot() {
        val content = snapshotDraft.trim()
        if (content.isEmpty()) {
            statusMessage = "快照内容不能为空"
            return
        }
        launchTask("快照创建成功") {
            repository.createSnapshot(content)
            showCreateSnapshotDialog = false
            snapshotDraft = ""
            refreshDashboard()
            if (historyTab == HistoryTab.SNAPSHOTS) refreshHistory()
        }
    }

    fun submitEvent() {
        val draft = eventDraft
        val description = draft.description.trim()
        if (description.isEmpty()) {
            statusMessage = "事件描述不能为空"
            return
        }

        launchTask(if (draft.id == null) "事件创建成功" else "事件更新成功") {
            val keywords = splitCsv(draft.keywordsCsv)
            val categories = splitCsv(draft.categoriesCsv)
            if (draft.id == null) {
                repository.createEvent(
                    CreateEventRequest(
                        date = draft.date.trim().ifBlank { null },
                        title = draft.title.trim().ifBlank { null },
                        description = description,
                        triggerKeywords = keywords,
                        categories = categories.ifEmpty { null },
                    )
                )
            } else {
                repository.updateEvent(
                    eventId = draft.id,
                    request = UpdateEventRequest(
                        title = draft.title.trim().ifBlank { null },
                        description = description,
                        triggerKeywords = keywords,
                        categories = categories,
                    )
                )
            }
            showEventDialog = false
            refreshHistory()
            refreshDashboard()
        }
    }

    fun deleteEvent(eventId: Int) {
        launchTask("事件已删除") {
            repository.deleteEvent(eventId)
            refreshHistory()
            refreshDashboard()
        }
    }

    fun refreshKeyRecords() {
        launchTask("关键记录已刷新") {
            keyRecords = repository.listKeyRecords(limit = 50)
        }
    }

    fun searchKeyRecords() {
        val query = keyRecordSearchQuery.trim()
        if (query.isBlank()) {
            refreshKeyRecords()
            return
        }
        launchTask("关键记录检索完成") {
            keyRecords = repository.searchKeyRecords(query)
        }
    }

    fun submitKeyRecord() {
        val draft = keyRecordDraft
        val title = draft.title.trim()
        val contentText = draft.contentText.trim()
        if (title.isEmpty()) {
            statusMessage = "关键记录标题不能为空"
            return
        }
        if (contentText.isEmpty()) {
            statusMessage = "关键记录内容不能为空"
            return
        }

        val type = normalizeEnum(draft.type, keyRecordTypeAllowed, "important_item")
        val status = normalizeEnum(draft.status, keyRecordStatusAllowed, "active")
        val source = normalizeEnum(draft.source, keyRecordSourceAllowed, "manual")
        val tags = splitCsv(draft.tagsCsv)

        launchTask(if (draft.id == null) "关键记录创建成功" else "关键记录更新成功") {
            if (draft.id == null) {
                repository.createKeyRecord(
                    CreateKeyRecordRequest(
                        type = type,
                        title = title,
                        contentText = contentText,
                        tags = tags,
                        startDate = draft.startDate.trim().ifBlank { null },
                        endDate = draft.endDate.trim().ifBlank { null },
                        status = status,
                        source = source,
                    )
                )
            } else {
                repository.updateKeyRecord(
                    recordId = draft.id,
                    request = UpdateKeyRecordRequest(
                        type = type,
                        title = title,
                        contentText = contentText,
                        tags = tags,
                        startDate = draft.startDate.trim().ifBlank { null },
                        endDate = draft.endDate.trim().ifBlank { null },
                        status = status,
                        source = source,
                    )
                )
            }
            showKeyRecordDialog = false
            refreshKeyRecords()
        }
    }

    fun deleteKeyRecord(recordId: Int) {
        launchTask("关键记录已删除") {
            repository.deleteKeyRecord(recordId)
            refreshKeyRecords()
        }
    }

    private fun launchTask(successMessage: String, block: suspend () -> Unit) {
        viewModelScope.launch {
            isBusy = true
            try {
                block()
                statusMessage = successMessage
            } catch (e: Exception) {
                statusMessage = "请求失败：${e.message ?: "未知错误"}"
            } finally {
                isBusy = false
            }
        }
    }

    private fun splitCsv(value: String): List<String> {
        return value.split(",")
            .map { it.trim() }
            .filter { it.isNotEmpty() }
            .distinct()
    }

    private fun parseJsonArrayToCsv(raw: String?): String {
        if (raw.isNullOrBlank()) return ""
        return try {
            val arr = JsonParser.parseString(raw).asJsonArray
            arr.mapNotNull { el -> if (el.isJsonNull) null else el.asString }
                .joinToString(", ")
        } catch (_: Exception) {
            raw
        }
    }

    private fun normalizeEnum(value: String, allowed: Set<String>, fallback: String): String {
        val normalized = value.trim().lowercase()
        return if (normalized in allowed) normalized else fallback
    }
}

