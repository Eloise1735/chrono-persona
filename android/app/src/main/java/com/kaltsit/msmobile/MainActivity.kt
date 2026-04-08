package com.kaltsit.msmobile

import android.os.Bundle
import androidx.activity.ComponentActivity
import androidx.activity.compose.setContent
import androidx.activity.enableEdgeToEdge
import androidx.activity.viewModels
import androidx.compose.foundation.BorderStroke
import androidx.compose.foundation.background
import androidx.compose.foundation.layout.Arrangement
import androidx.compose.foundation.layout.Column
import androidx.compose.foundation.layout.Row
import androidx.compose.foundation.layout.Spacer
import androidx.compose.foundation.layout.fillMaxSize
import androidx.compose.foundation.layout.fillMaxWidth
import androidx.compose.foundation.layout.height
import androidx.compose.foundation.layout.padding
import androidx.compose.foundation.layout.size
import androidx.compose.foundation.layout.width
import androidx.compose.foundation.lazy.LazyColumn
import androidx.compose.foundation.lazy.items
import androidx.compose.foundation.rememberScrollState
import androidx.compose.foundation.verticalScroll
import androidx.compose.material3.AlertDialog
import androidx.compose.material3.Button
import androidx.compose.material3.Card
import androidx.compose.material3.CardDefaults
import androidx.compose.material3.Checkbox
import androidx.compose.material3.CircularProgressIndicator
import androidx.compose.material3.FilterChip
import androidx.compose.material3.MaterialTheme
import androidx.compose.material3.OutlinedButton
import androidx.compose.material3.OutlinedTextField
import androidx.compose.material3.Scaffold
import androidx.compose.material3.SnackbarHost
import androidx.compose.material3.SnackbarHostState
import androidx.compose.material3.Text
import androidx.compose.material3.TextButton
import androidx.compose.runtime.Composable
import androidx.compose.runtime.LaunchedEffect
import androidx.compose.runtime.remember
import androidx.compose.ui.Alignment
import androidx.compose.ui.Modifier
import androidx.compose.ui.text.font.FontWeight
import androidx.compose.ui.text.style.TextOverflow
import androidx.compose.ui.unit.dp
import com.kaltsit.msmobile.data.AutomationRunItem
import com.kaltsit.msmobile.data.EventAnchor
import com.kaltsit.msmobile.data.KeyRecord
import com.kaltsit.msmobile.data.Snapshot
import com.kaltsit.msmobile.ui.theme.Accent
import com.kaltsit.msmobile.ui.theme.Border
import com.kaltsit.msmobile.ui.theme.KaltsitTheme
import com.kaltsit.msmobile.ui.theme.Surface

class MainActivity : ComponentActivity() {
    private val viewModel: MainViewModel by viewModels()

    override fun onCreate(savedInstanceState: Bundle?) {
        super.onCreate(savedInstanceState)
        enableEdgeToEdge()
        setContent {
            KaltsitTheme {
                AppScreen(viewModel)
            }
        }
    }
}

@Composable
private fun AppScreen(vm: MainViewModel) {
    val snackbarHostState = remember { SnackbarHostState() }
    LaunchedEffect(vm.statusMessage) {
        if (vm.statusMessage.isNotBlank()) {
            snackbarHostState.showSnackbar(vm.statusMessage)
        }
    }

    Scaffold(
        snackbarHost = { SnackbarHost(snackbarHostState) },
        bottomBar = {
            Row(
                modifier = Modifier
                    .fillMaxWidth()
                    .background(Surface)
                    .padding(horizontal = 6.dp, vertical = 4.dp),
                horizontalArrangement = Arrangement.SpaceEvenly,
            ) {
                BottomItem(AppTab.DASHBOARD, vm.selectedTab, "首页", vm::selectTab)
                BottomItem(AppTab.HISTORY, vm.selectedTab, "历史", vm::selectTab)
                BottomItem(AppTab.KEY_RECORDS, vm.selectedTab, "记录", vm::selectTab)
                BottomItem(AppTab.SETTINGS, vm.selectedTab, "设置", vm::selectTab)
            }
        },
    ) { innerPadding ->
        Column(
            modifier = Modifier
                .fillMaxSize()
                .background(MaterialTheme.colorScheme.background)
                .padding(innerPadding)
                .padding(12.dp),
        ) {
            Row(verticalAlignment = Alignment.CenterVertically) {
                Text(
                    text = "凯尔希状态机 MVP",
                    style = MaterialTheme.typography.titleLarge,
                    fontWeight = FontWeight.Bold,
                )
                Spacer(Modifier.width(10.dp))
                if (vm.isBusy) {
                    CircularProgressIndicator(modifier = Modifier.size(16.dp), strokeWidth = 2.dp)
                }
            }

            Spacer(Modifier.height(12.dp))

            when (vm.selectedTab) {
                AppTab.DASHBOARD -> DashboardScreen(vm)
                AppTab.HISTORY -> HistoryScreen(vm)
                AppTab.KEY_RECORDS -> KeyRecordScreen(vm)
                AppTab.SETTINGS -> SettingsScreen(vm)
            }
        }
    }

    if (vm.showEventDialog) {
        EventDialog(
            draft = vm.eventDraft,
            onDismiss = vm::closeEventDialog,
            onChange = vm::updateEventDraft,
            onSubmit = vm::submitEvent,
            onDelete = {
                val id = vm.eventDraft.id
                if (id != null) vm.deleteEvent(id)
                vm.closeEventDialog()
            },
        )
    }

    if (vm.showCreateSnapshotDialog) {
        CreateSnapshotDialog(
            content = vm.snapshotDraft,
            onChange = vm::onSnapshotDraftChange,
            onDismiss = { vm.toggleCreateSnapshotDialog(false) },
            onSubmit = vm::submitSnapshot,
        )
    }

    if (vm.showKeyRecordDialog) {
        KeyRecordDialog(
            draft = vm.keyRecordDraft,
            onDismiss = vm::closeKeyRecordDialog,
            onChange = vm::updateKeyRecordDraft,
            onSubmit = vm::submitKeyRecord,
            onDelete = {
                val id = vm.keyRecordDraft.id
                if (id != null) vm.deleteKeyRecord(id)
                vm.closeKeyRecordDialog()
            },
        )
    }
}

@Composable
private fun BottomItem(
    tab: AppTab,
    current: AppTab,
    label: String,
    onClick: (AppTab) -> Unit,
) {
    TextButton(onClick = { onClick(tab) }) {
        Text(
            text = label,
            color = if (tab == current) Accent else MaterialTheme.colorScheme.onSurface,
            fontWeight = if (tab == current) FontWeight.SemiBold else FontWeight.Normal,
        )
    }
}

@Composable
private fun DashboardScreen(vm: MainViewModel) {
    Column(
        verticalArrangement = Arrangement.spacedBy(10.dp),
        modifier = Modifier.verticalScroll(rememberScrollState()),
    ) {
        Row(horizontalArrangement = Arrangement.spacedBy(8.dp)) {
            Button(onClick = vm::refreshDashboard) { Text("刷新") }
            OutlinedButton(onClick = { vm.toggleCreateSnapshotDialog(true) }) { Text("创建快照") }
            OutlinedButton(onClick = vm::openCreateEventDialog) { Text("添加事件") }
        }

        SnapshotCard(snapshot = vm.latestSnapshot)
        AutomationCard(item = vm.latestAutomation)
    }
}

@Composable
private fun SnapshotCard(snapshot: Snapshot?) {
    AppCard(title = "当前状态快照") {
        if (snapshot == null) {
            Text("暂无快照")
            return@AppCard
        }
        Text("类型: ${snapshot.type}  时间: ${snapshot.createdAt}", color = MaterialTheme.colorScheme.secondary)
        Spacer(Modifier.height(8.dp))
        Text(snapshot.content)
    }
}

@Composable
private fun AutomationCard(item: AutomationRunItem?) {
    AppCard(title = "最近自动化执行") {
        if (item == null) {
            Text("暂无记录")
            return@AppCard
        }
        Text("状态: ${item.status ?: "未知"}")
        Text("触发: ${item.trigger ?: "-"}")
        Text("时间: ${item.runAt ?: item.createdAt ?: "-"}")
        val vectorized = item.report?.vectorSync?.get("vectorized_events")
        if (vectorized != null) {
            Text("向量同步事件数: $vectorized", color = MaterialTheme.colorScheme.secondary)
        }
    }
}

@Composable
private fun HistoryScreen(vm: MainViewModel) {
    Column(verticalArrangement = Arrangement.spacedBy(10.dp), modifier = Modifier.fillMaxSize()) {
        Row(horizontalArrangement = Arrangement.spacedBy(8.dp)) {
            FilterChip(
                selected = vm.historyTab == HistoryTab.SNAPSHOTS,
                onClick = { vm.switchHistoryTab(HistoryTab.SNAPSHOTS) },
                label = { Text("快照") },
            )
            FilterChip(
                selected = vm.historyTab == HistoryTab.EVENTS,
                onClick = { vm.switchHistoryTab(HistoryTab.EVENTS) },
                label = { Text("事件") },
            )
        }

        OutlinedTextField(
            value = vm.historySearchQuery,
            onValueChange = vm::onHistorySearchChange,
            modifier = Modifier.fillMaxWidth(),
            label = { Text("搜索关键字") },
            singleLine = true,
        )

        Row(horizontalArrangement = Arrangement.spacedBy(8.dp)) {
            Button(onClick = vm::searchHistory) { Text("检索") }
            OutlinedButton(onClick = vm::refreshHistory) { Text("刷新") }
            if (vm.historyTab == HistoryTab.EVENTS) {
                OutlinedButton(onClick = vm::openCreateEventDialog) { Text("新增事件") }
            }
        }

        if (vm.historyTab == HistoryTab.EVENTS) {
            Row(verticalAlignment = Alignment.CenterVertically) {
                Checkbox(checked = vm.includeArchivedEvents, onCheckedChange = vm::onIncludeArchivedChange)
                Text("显示已归档事件")
            }
        }

        if (vm.historyTab == HistoryTab.SNAPSHOTS) {
            SnapshotList(snapshots = vm.snapshots, modifier = Modifier.weight(1f))
        } else {
            EventList(
                events = vm.events,
                onEdit = vm::openEditEventDialog,
                onDelete = vm::deleteEvent,
                modifier = Modifier.weight(1f),
            )
        }
    }
}

@Composable
private fun SnapshotList(snapshots: List<Snapshot>, modifier: Modifier = Modifier) {
    if (snapshots.isEmpty()) {
        Text("暂无数据", modifier = modifier)
        return
    }
    LazyColumn(verticalArrangement = Arrangement.spacedBy(8.dp), modifier = modifier.fillMaxWidth()) {
        items(snapshots, key = { it.id }) { item ->
            AppCard(title = "#${item.id} ${item.type}") {
                Text(item.createdAt, color = MaterialTheme.colorScheme.secondary)
                Spacer(Modifier.height(6.dp))
                Text(item.content, maxLines = 6, overflow = TextOverflow.Ellipsis)
            }
        }
    }
}

@Composable
private fun EventList(
    events: List<EventAnchor>,
    onEdit: (EventAnchor) -> Unit,
    onDelete: (Int) -> Unit,
    modifier: Modifier = Modifier,
) {
    if (events.isEmpty()) {
        Text("暂无数据", modifier = modifier)
        return
    }
    LazyColumn(verticalArrangement = Arrangement.spacedBy(8.dp), modifier = modifier.fillMaxWidth()) {
        items(events, key = { it.id }) { item ->
            AppCard(title = "#${item.id} ${item.title.ifBlank { "未命名事件" }}") {
                Text("${item.date}  ${item.source}", color = MaterialTheme.colorScheme.secondary)
                Spacer(Modifier.height(6.dp))
                Text(item.description, maxLines = 5, overflow = TextOverflow.Ellipsis)
                Spacer(Modifier.height(8.dp))
                Row(horizontalArrangement = Arrangement.spacedBy(8.dp)) {
                    OutlinedButton(onClick = { onEdit(item) }) { Text("编辑") }
                    OutlinedButton(onClick = { onDelete(item.id) }) { Text("删除") }
                }
            }
        }
    }
}

@Composable
private fun KeyRecordScreen(vm: MainViewModel) {
    Column(verticalArrangement = Arrangement.spacedBy(10.dp), modifier = Modifier.fillMaxSize()) {
        OutlinedTextField(
            value = vm.keyRecordSearchQuery,
            onValueChange = vm::onKeyRecordSearchChange,
            modifier = Modifier.fillMaxWidth(),
            label = { Text("搜索关键记录") },
            singleLine = true,
        )

        Row(horizontalArrangement = Arrangement.spacedBy(8.dp)) {
            Button(onClick = vm::searchKeyRecords) { Text("检索") }
            OutlinedButton(onClick = vm::refreshKeyRecords) { Text("刷新") }
            OutlinedButton(onClick = vm::openCreateKeyRecordDialog) { Text("新增记录") }
        }

        KeyRecordList(
            items = vm.keyRecords,
            onEdit = vm::openEditKeyRecordDialog,
            onDelete = vm::deleteKeyRecord,
            modifier = Modifier.weight(1f),
        )
    }
}

@Composable
private fun KeyRecordList(
    items: List<KeyRecord>,
    onEdit: (KeyRecord) -> Unit,
    onDelete: (Int) -> Unit,
    modifier: Modifier = Modifier,
) {
    if (items.isEmpty()) {
        Text("暂无数据", modifier = modifier)
        return
    }
    LazyColumn(verticalArrangement = Arrangement.spacedBy(8.dp), modifier = modifier.fillMaxWidth()) {
        items(items, key = { it.id }) { item ->
            AppCard(title = "#${item.id} ${item.title}") {
                Text("${item.type} | ${item.status} | ${item.updatedAt}", color = MaterialTheme.colorScheme.secondary)
                Spacer(Modifier.height(6.dp))
                Text(item.contentText, maxLines = 5, overflow = TextOverflow.Ellipsis)
                Spacer(Modifier.height(8.dp))
                Row(horizontalArrangement = Arrangement.spacedBy(8.dp)) {
                    OutlinedButton(onClick = { onEdit(item) }) { Text("编辑") }
                    OutlinedButton(onClick = { onDelete(item.id) }) { Text("删除") }
                }
            }
        }
    }
}

@Composable
private fun SettingsScreen(vm: MainViewModel) {
    Column(verticalArrangement = Arrangement.spacedBy(10.dp)) {
        AppCard(title = "本地连接设置") {
            Text("当前地址: ${vm.savedBaseUrl}", color = MaterialTheme.colorScheme.secondary)
            Spacer(Modifier.height(8.dp))
            OutlinedTextField(
                value = vm.baseUrlInput,
                onValueChange = vm::onBaseUrlInputChange,
                modifier = Modifier.fillMaxWidth(),
                label = { Text("后端地址") },
                supportingText = { Text("示例: http://192.168.1.20:8000") },
                singleLine = true,
            )
            Spacer(Modifier.height(8.dp))
            Row(horizontalArrangement = Arrangement.spacedBy(8.dp)) {
                Button(onClick = vm::saveBaseUrl) { Text("保存") }
                OutlinedButton(onClick = vm::testConnection) { Text("测试连接") }
                OutlinedButton(onClick = vm::refreshAll) { Text("全部刷新") }
            }
        }
    }
}

@Composable
private fun AppCard(title: String, content: @Composable () -> Unit) {
    Card(
        colors = CardDefaults.cardColors(containerColor = Surface),
        border = BorderStroke(1.dp, Border),
        modifier = Modifier.fillMaxWidth(),
    ) {
        Column(modifier = Modifier.padding(12.dp)) {
            Text(text = title, fontWeight = FontWeight.SemiBold, color = Accent)
            Spacer(Modifier.height(8.dp))
            content()
        }
    }
}

@Composable
private fun EventDialog(
    draft: EventDraft,
    onDismiss: () -> Unit,
    onChange: (date: String, title: String, description: String, keywordsCsv: String, categoriesCsv: String) -> Unit,
    onSubmit: () -> Unit,
    onDelete: () -> Unit,
) {
    AlertDialog(
        onDismissRequest = onDismiss,
        confirmButton = {
            Button(onClick = onSubmit) {
                Text(if (draft.id == null) "创建" else "保存")
            }
        },
        dismissButton = {
            Row(horizontalArrangement = Arrangement.spacedBy(6.dp)) {
                if (draft.id != null) {
                    OutlinedButton(onClick = onDelete) { Text("删除") }
                }
                TextButton(onClick = onDismiss) { Text("取消") }
            }
        },
        title = { Text(if (draft.id == null) "新增事件" else "编辑事件") },
        text = {
            Column(verticalArrangement = Arrangement.spacedBy(8.dp)) {
                OutlinedTextField(
                    value = draft.date,
                    onValueChange = { onChange(it, draft.title, draft.description, draft.keywordsCsv, draft.categoriesCsv) },
                    label = { Text("日期(YYYY-MM-DD)") },
                    singleLine = true,
                )
                OutlinedTextField(
                    value = draft.title,
                    onValueChange = { onChange(draft.date, it, draft.description, draft.keywordsCsv, draft.categoriesCsv) },
                    label = { Text("标题(可选)") },
                    singleLine = true,
                )
                OutlinedTextField(
                    value = draft.description,
                    onValueChange = { onChange(draft.date, draft.title, it, draft.keywordsCsv, draft.categoriesCsv) },
                    label = { Text("描述") },
                )
                OutlinedTextField(
                    value = draft.keywordsCsv,
                    onValueChange = { onChange(draft.date, draft.title, draft.description, it, draft.categoriesCsv) },
                    label = { Text("触发关键词(逗号分隔)") },
                )
                OutlinedTextField(
                    value = draft.categoriesCsv,
                    onValueChange = { onChange(draft.date, draft.title, draft.description, draft.keywordsCsv, it) },
                    label = { Text("分类(逗号分隔)") },
                )
            }
        },
    )
}

@Composable
private fun KeyRecordDialog(
    draft: KeyRecordDraft,
    onDismiss: () -> Unit,
    onChange: (
        type: String,
        title: String,
        contentText: String,
        tagsCsv: String,
        startDate: String,
        endDate: String,
        status: String,
        source: String,
    ) -> Unit,
    onSubmit: () -> Unit,
    onDelete: () -> Unit,
) {
    AlertDialog(
        onDismissRequest = onDismiss,
        confirmButton = {
            Button(onClick = onSubmit) {
                Text(if (draft.id == null) "创建" else "保存")
            }
        },
        dismissButton = {
            Row(horizontalArrangement = Arrangement.spacedBy(6.dp)) {
                if (draft.id != null) {
                    OutlinedButton(onClick = onDelete) { Text("删除") }
                }
                TextButton(onClick = onDismiss) { Text("取消") }
            }
        },
        title = { Text(if (draft.id == null) "新增关键记录" else "编辑关键记录") },
        text = {
            Column(verticalArrangement = Arrangement.spacedBy(8.dp)) {
                OutlinedTextField(
                    value = draft.type,
                    onValueChange = {
                        onChange(it, draft.title, draft.contentText, draft.tagsCsv, draft.startDate, draft.endDate, draft.status, draft.source)
                    },
                    label = { Text("类型") },
                    supportingText = { Text("important_date / important_item / key_collaboration / medical_advice") },
                )
                OutlinedTextField(
                    value = draft.title,
                    onValueChange = {
                        onChange(draft.type, it, draft.contentText, draft.tagsCsv, draft.startDate, draft.endDate, draft.status, draft.source)
                    },
                    label = { Text("标题") },
                )
                OutlinedTextField(
                    value = draft.contentText,
                    onValueChange = {
                        onChange(draft.type, draft.title, it, draft.tagsCsv, draft.startDate, draft.endDate, draft.status, draft.source)
                    },
                    label = { Text("内容") },
                )
                OutlinedTextField(
                    value = draft.tagsCsv,
                    onValueChange = {
                        onChange(draft.type, draft.title, draft.contentText, it, draft.startDate, draft.endDate, draft.status, draft.source)
                    },
                    label = { Text("标签(逗号分隔)") },
                )
                OutlinedTextField(
                    value = draft.startDate,
                    onValueChange = {
                        onChange(draft.type, draft.title, draft.contentText, draft.tagsCsv, it, draft.endDate, draft.status, draft.source)
                    },
                    label = { Text("开始日期(可选)") },
                    supportingText = { Text("YYYY-MM-DD") },
                )
                OutlinedTextField(
                    value = draft.endDate,
                    onValueChange = {
                        onChange(draft.type, draft.title, draft.contentText, draft.tagsCsv, draft.startDate, it, draft.status, draft.source)
                    },
                    label = { Text("结束日期(可选)") },
                    supportingText = { Text("YYYY-MM-DD") },
                )
                OutlinedTextField(
                    value = draft.status,
                    onValueChange = {
                        onChange(draft.type, draft.title, draft.contentText, draft.tagsCsv, draft.startDate, draft.endDate, it, draft.source)
                    },
                    label = { Text("状态") },
                    supportingText = { Text("active / archived") },
                )
                OutlinedTextField(
                    value = draft.source,
                    onValueChange = {
                        onChange(draft.type, draft.title, draft.contentText, draft.tagsCsv, draft.startDate, draft.endDate, draft.status, it)
                    },
                    label = { Text("来源") },
                    supportingText = { Text("manual / conversation / generated") },
                )
            }
        },
    )
}

@Composable
private fun CreateSnapshotDialog(
    content: String,
    onChange: (String) -> Unit,
    onDismiss: () -> Unit,
    onSubmit: () -> Unit,
) {
    AlertDialog(
        onDismissRequest = onDismiss,
        title = { Text("创建快照") },
        text = {
            OutlinedTextField(
                value = content,
                onValueChange = onChange,
                label = { Text("快照内容") },
                modifier = Modifier.fillMaxWidth(),
            )
        },
        confirmButton = {
            Button(onClick = onSubmit) { Text("保存") }
        },
        dismissButton = {
            TextButton(onClick = onDismiss) { Text("取消") }
        },
    )
}

