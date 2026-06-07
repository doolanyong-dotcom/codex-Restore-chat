param(
  [switch]$InstallShortcutOnly,
  [switch]$SmokeTest
)

Set-StrictMode -Version Latest
$ErrorActionPreference = 'Stop'

Add-Type -AssemblyName System.Windows.Forms
Add-Type -AssemblyName System.Drawing
Add-Type -AssemblyName Microsoft.VisualBasic
[System.Windows.Forms.Application]::EnableVisualStyles()
[System.Windows.Forms.Application]::SetCompatibleTextRenderingDefault($false)

$script:UiScriptPath = $MyInvocation.MyCommand.Path
$script:ToolRoot = Split-Path -Parent $MyInvocation.MyCommand.Path
$script:BackendPath = Join-Path $script:ToolRoot 'sync_backend.py'
$script:AssetsDir = Join-Path $script:ToolRoot 'assets'
$script:IconPath = Join-Path $script:AssetsDir 'codex-history-sync.ico'
$script:FallbackIconLocation = 'C:\Windows\System32\imageres.dll,15'
$script:ShortcutName = 'Codex 对话同步工具.lnk'
$script:BackupMap = @{}
$script:LatestState = $null
$script:ShareState = $null
$script:IsBusy = $false
$script:CodexHomeOverride = $null
$script:ManualBackupDone = $false

$colorInk = [System.Drawing.Color]::FromArgb(23, 31, 42)
$colorInkSoft = [System.Drawing.Color]::FromArgb(55, 65, 81)
$colorText = [System.Drawing.Color]::FromArgb(25, 33, 44)
$colorMuted = [System.Drawing.Color]::FromArgb(86, 101, 117)
$colorSurface = [System.Drawing.Color]::FromArgb(244, 246, 249)
$colorPanel = [System.Drawing.Color]::White
$colorBorder = [System.Drawing.Color]::FromArgb(216, 224, 232)
$colorPrimary = [System.Drawing.Color]::FromArgb(37, 99, 235)
$colorPrimaryHover = [System.Drawing.Color]::FromArgb(29, 78, 216)
$colorAccent = [System.Drawing.Color]::FromArgb(15, 118, 110)
$colorAccentHover = [System.Drawing.Color]::FromArgb(13, 95, 88)
$colorDanger = [System.Drawing.Color]::FromArgb(185, 28, 28)
$colorDangerHover = [System.Drawing.Color]::FromArgb(153, 27, 27)
$colorSoftTeal = [System.Drawing.Color]::FromArgb(230, 247, 245)
$colorSoftInfo = [System.Drawing.Color]::FromArgb(236, 242, 255)
$colorSoftWarm = [System.Drawing.Color]::FromArgb(255, 247, 237)
$colorDisabled = [System.Drawing.Color]::FromArgb(229, 235, 242)

$mainStatusLabel = $null
$statusHintLabel = $null
$progressBar = $null
$logBox = $null
$refreshButton = $null
$syncButton = $null
$backupButton = $null
$chooseCodexFolderButton = $null
$restoreButton = $null
$restoreLatestButton = $null
$shortcutButton = $null
$openBackupsButton = $null
$openCodexFolderButton = $null
$shareRefreshButton = $null
$shareEnableButton = $null
$shareDisableButton = $null
$shareOnceButton = $null
$openShareLogButton = $null
$restoreStatusLabel = $null
$providerLabel = $null
$modelLabel = $null
$summaryLabel = $null
$repairLabel = $null
$visibilityLabel = $null
$pathLabel = $null
$providersView = $null
$backupList = $null
$shareStatusLabel = $null
$shareLogLabel = $null
$shareStartupLabel = $null
$shareHintLabel = $null
$toolTip = $null

function Invoke-Backend {
  param(
    [Parameter(Mandatory = $true)]
    [string[]]$Arguments
  )

  if (-not (Test-Path -LiteralPath $script:BackendPath)) {
    throw "缺少后端脚本: $script:BackendPath"
  }

  $backendArguments = @()
  if ($script:CodexHomeOverride) {
    $backendArguments += @('--codex-home', $script:CodexHomeOverride)
  }
  $backendArguments += $Arguments

  $output = & py -3 $script:BackendPath @backendArguments 2>&1
  $exitCode = $LASTEXITCODE
  $text = (($output | ForEach-Object { "$_" }) -join [Environment]::NewLine).Trim()
  if (-not $text) {
    throw '后端没有返回任何内容。'
  }

  try {
    $json = $text | ConvertFrom-Json
  } catch {
    throw "后端 JSON 解析失败。`r`n原始错误: $($_.Exception.Message)`r`n返回内容:`r`n$text"
  }

  if ($exitCode -ne 0 -or -not $json.ok) {
    if ($json.error) {
      throw [string]$json.error
    }
    throw "后端执行失败。`r`n$text"
  }

  return $json
}

function Get-ShortcutIconLocation {
  if (Test-Path -LiteralPath $script:IconPath) {
    return $script:IconPath
  }

  return $script:FallbackIconLocation
}

function New-DesktopShortcut {
  $desktopPath = [Environment]::GetFolderPath('Desktop')
  $shortcutPath = Join-Path $desktopPath $script:ShortcutName
  $targetPath = Join-Path $PSHOME 'powershell.exe'
  $arguments = "-NoProfile -ExecutionPolicy Bypass -Sta -WindowStyle Hidden -File `"$script:UiScriptPath`""

  $shell = New-Object -ComObject WScript.Shell
  $shortcut = $shell.CreateShortcut($shortcutPath)
  $shortcut.TargetPath = $targetPath
  $shortcut.Arguments = $arguments
  $shortcut.WorkingDirectory = $script:ToolRoot
  $shortcut.IconLocation = Get-ShortcutIconLocation
  $shortcut.Description = 'Codex 对话同步与恢复'
  $shortcut.Save()

  return $shortcutPath
}

if ($InstallShortcutOnly) {
  $createdShortcut = New-DesktopShortcut
  Write-Output "桌面入口已创建: $createdShortcut"
  exit 0
}

function Set-AppIcon {
  param([System.Windows.Forms.Form]$TargetForm)

  if (-not (Test-Path -LiteralPath $script:IconPath)) {
    return
  }

  try {
    $TargetForm.Icon = New-Object System.Drawing.Icon($script:IconPath)
  } catch {
    # 图标损坏不影响工具主体启动，桌面入口仍可回退到系统图标。
  }
}

function New-Label {
  param(
    [string]$Text,
    [int]$X,
    [int]$Y,
    [int]$Width = 400,
    [int]$Height = 24,
    [int]$Size = 9,
    [System.Drawing.Color]$Color = $colorText,
    [System.Drawing.FontStyle]$Style = [System.Drawing.FontStyle]::Regular,
    [System.Drawing.ContentAlignment]$Align = [System.Drawing.ContentAlignment]::MiddleLeft
  )

  $label = New-Object System.Windows.Forms.Label
  $label.Text = $Text
  $label.Location = New-Object System.Drawing.Point($X, $Y)
  $label.Size = New-Object System.Drawing.Size($Width, $Height)
  $label.ForeColor = $Color
  $label.Font = New-Object System.Drawing.Font('Microsoft YaHei UI', $Size, $Style)
  $label.TextAlign = $Align
  $label.AutoEllipsis = $true
  return $label
}

function New-Panel {
  param(
    [int]$X,
    [int]$Y,
    [int]$Width,
    [int]$Height,
    [System.Drawing.Color]$BackColor = $colorPanel
  )

  $panel = New-Object System.Windows.Forms.Panel
  $panel.Location = New-Object System.Drawing.Point($X, $Y)
  $panel.Size = New-Object System.Drawing.Size($Width, $Height)
  $panel.BackColor = $BackColor
  $panel.BorderStyle = 'FixedSingle'
  return $panel
}

function New-Button {
  param(
    [string]$Text,
    [int]$X,
    [int]$Y,
    [int]$Width,
    [int]$Height = 40,
    [ValidateSet('Primary', 'Secondary', 'Accent', 'Danger')]
    [string]$Kind = 'Secondary'
  )

  $button = New-Object System.Windows.Forms.Button
  $button.Text = $Text
  $button.Location = New-Object System.Drawing.Point($X, $Y)
  $button.Size = New-Object System.Drawing.Size($Width, $Height)
  $button.FlatStyle = 'Flat'
  $button.FlatAppearance.BorderSize = 1
  $button.Font = New-Object System.Drawing.Font('Microsoft YaHei UI', 9, [System.Drawing.FontStyle]::Regular)
  $button.Cursor = [System.Windows.Forms.Cursors]::Hand
  $button.UseVisualStyleBackColor = $false

  switch ($Kind) {
    'Primary' {
      $button.BackColor = $colorPrimary
      $button.ForeColor = [System.Drawing.Color]::White
      $button.FlatAppearance.BorderColor = $colorPrimary
      $button.FlatAppearance.MouseOverBackColor = $colorPrimaryHover
      $button.FlatAppearance.MouseDownBackColor = $colorPrimaryHover
    }
    'Accent' {
      $button.BackColor = $colorAccent
      $button.ForeColor = [System.Drawing.Color]::White
      $button.FlatAppearance.BorderColor = $colorAccent
      $button.FlatAppearance.MouseOverBackColor = $colorAccentHover
      $button.FlatAppearance.MouseDownBackColor = $colorAccentHover
    }
    'Danger' {
      $button.BackColor = $colorDanger
      $button.ForeColor = [System.Drawing.Color]::White
      $button.FlatAppearance.BorderColor = $colorDanger
      $button.FlatAppearance.MouseOverBackColor = $colorDangerHover
      $button.FlatAppearance.MouseDownBackColor = $colorDangerHover
    }
    default {
      $button.BackColor = $colorPanel
      $button.ForeColor = $colorText
      $button.FlatAppearance.BorderColor = $colorBorder
      $button.FlatAppearance.MouseOverBackColor = $colorSoftInfo
      $button.FlatAppearance.MouseDownBackColor = $colorSoftTeal
    }
  }

  return $button
}

function Set-ControlTip {
  param(
    [System.Windows.Forms.Control]$Control,
    [string]$Text
  )

  if ($toolTip -and $Control) {
    $toolTip.SetToolTip($Control, $Text)
  }
}

function Append-Log {
  param([string]$Message)

  if (-not $logBox) {
    return
  }

  $timestamp = Get-Date -Format 'HH:mm:ss'
  $logBox.AppendText("[$timestamp] $Message`r`n")
  $logBox.SelectionStart = $logBox.TextLength
  $logBox.ScrollToCaret()
}

function Format-Counts {
  param($Counts)

  if (-not $Counts -or $Counts.Count -eq 0) {
    return '无'
  }

  return (($Counts | ForEach-Object { "$($_.provider)=$($_.count)" }) -join ', ')
}

function Format-ModelCounts {
  param($Counts)

  if (-not $Counts -or $Counts.Count -eq 0) {
    return '无'
  }

  return (($Counts | ForEach-Object { "$($_.model)=$($_.count)" }) -join ', ')
}

function Format-Duration {
  param($Milliseconds)

  if ($null -eq $Milliseconds) {
    return '0 秒'
  }

  $seconds = [Math]::Round(([double]$Milliseconds / 1000), 1)
  return "$seconds 秒"
}

function Format-NullableCount {
  param($Value)

  if ($null -eq $Value) {
    return '不适用'
  }

  return [string]$Value
}

function Get-FriendlyStatus {
  param($Status)

  if (-not $Status) {
    return '正在读取 Codex 状态...'
  }

  if ([int]$Status.movable_threads -le 0) {
    return '状态良好：当前聊天记录已经在 Codex 可见位置。'
  }

  $parts = @()
  if ([int]$Status.movable_database_threads -gt 0) {
    $parts += "$($Status.movable_database_threads) 条归属"
  }
  if ($null -ne $Status.model_movable_threads -and [int]$Status.model_movable_threads -gt 0) {
    $parts += "$($Status.model_movable_threads) 条模型"
  }
  if ([int]$Status.movable_session_threads -gt 0) {
    $parts += "$($Status.movable_session_threads) 个会话文件"
  }
  if ([int]$Status.missing_session_index_entries -gt 0) {
    $parts += "$($Status.missing_session_index_entries) 条侧边栏索引"
  }
  if ([int]$Status.visibility_movable_threads -gt 0) {
    $parts += "$($Status.visibility_movable_threads) 条可见性"
  }

  return "建议恢复：" + ($parts -join '，')
}

function Get-PrimaryHint {
  param($Status)

  if (-not $script:ManualBackupDone) {
    return '请先点“手动备份”。看到备份完成提示后，再进行恢复、还原或共享操作。'
  }

  if (-not $Status) {
    return '默认先查找本机 Codex 数据目录；如果找不到，会提示你手动选择。'
  }

  if ([int]$Status.movable_threads -gt 0) {
    return '点击“一键恢复”会再保存安全备份，并把历史记录挂回当前账号/API/model（模型）可见位置。'
  }

  return '无需恢复。经常切换账号或 API 时，可以开启“共享模式”持续保持可见。'
}

function Get-ShareStatusText {
  param($Status)

  if (-not $Status) {
    return '正在读取共享模式...'
  }

  if ($Status.enabled -and $Status.running) {
    return "共享模式运行中，后台 PID $($Status.pid)"
  }
  if ($Status.enabled) {
    return '共享模式已开启，当前后台进程未运行'
  }
  return '共享模式未开启'
}

function Update-ActionAvailability {
  $actionButtons = @(
    $refreshButton,
    $syncButton,
    $backupButton,
    $chooseCodexFolderButton,
    $restoreButton,
    $restoreLatestButton,
    $shortcutButton,
    $openBackupsButton,
    $openCodexFolderButton,
    $shareRefreshButton,
    $shareEnableButton,
    $shareDisableButton,
    $shareOnceButton,
    $openShareLogButton
  )

  if ($script:IsBusy) {
    foreach ($button in $actionButtons) {
      if ($button) {
        $button.Enabled = $false
      }
    }
    return
  }

  $hasState = $null -ne $script:LatestState
  $hasMovable = $hasState -and [int]$script:LatestState.movable_threads -gt 0
  $hasBackups = $hasState -and $script:LatestState.backups -and $script:LatestState.backups.Count -gt 0
  $hasSelectedBackup = $backupList -and $backupList.SelectedItem -ne $null
  $shareKnown = $null -ne $script:ShareState
  $shareEnabled = $shareKnown -and [bool]$script:ShareState.enabled
  $shareRunning = $shareKnown -and [bool]$script:ShareState.running
  $manualReady = [bool]$script:ManualBackupDone

  if ($refreshButton) { $refreshButton.Enabled = $true }
  if ($syncButton) { $syncButton.Enabled = $hasMovable -and $manualReady }
  if ($backupButton) { $backupButton.Enabled = $hasState }
  if ($chooseCodexFolderButton) { $chooseCodexFolderButton.Enabled = $true }
  if ($restoreButton) { $restoreButton.Enabled = $hasSelectedBackup -and $manualReady }
  if ($restoreLatestButton) { $restoreLatestButton.Enabled = $hasBackups -and $manualReady }
  if ($shortcutButton) { $shortcutButton.Enabled = $true }
  if ($openBackupsButton) { $openBackupsButton.Enabled = $true }
  if ($openCodexFolderButton) { $openCodexFolderButton.Enabled = $hasState }
  if ($shareRefreshButton) { $shareRefreshButton.Enabled = $true }
  if ($shareEnableButton) { $shareEnableButton.Enabled = (-not $shareEnabled) -and $manualReady }
  if ($shareDisableButton) { $shareDisableButton.Enabled = $shareEnabled -or $shareRunning }
  if ($shareOnceButton) { $shareOnceButton.Enabled = $hasState -and $manualReady }
  if ($openShareLogButton) { $openShareLogButton.Enabled = $shareKnown }
}

function Set-Busy {
  param(
    [bool]$Busy,
    [string]$Message = ''
  )

  $script:IsBusy = $Busy

  if ($Busy) {
    if ($mainStatusLabel) { $mainStatusLabel.Text = $Message }
    if ($statusHintLabel) { $statusHintLabel.Text = '处理中，请保持 Codex Desktop 和本工具不要被强制关闭。' }
    if ($progressBar) {
      $progressBar.Style = 'Marquee'
      $progressBar.Visible = $true
    }
  } else {
    if ($progressBar) {
      $progressBar.Style = 'Blocks'
      $progressBar.Visible = $false
    }
    if ($mainStatusLabel) { $mainStatusLabel.Text = Get-FriendlyStatus $script:LatestState }
    if ($statusHintLabel) { $statusHintLabel.Text = Get-PrimaryHint $script:LatestState }
  }

  Update-ActionAvailability
}

function Refresh-State {
  try {
    $status = Invoke-Backend @('--json', 'status')
  } catch {
    if (Should-PromptForCodexHome -Message $_.Exception.Message) {
      if (Request-CodexHomeDirectory -Reason $_.Exception.Message) {
        $status = Invoke-Backend @('--json', 'status')
      } else {
        throw
      }
    } else {
      throw
    }
  }

  Apply-State $status
  Append-Log "恢复状态已刷新：$(Get-FriendlyStatus $status)"
}

function Refresh-ShareState {
  $status = Invoke-Backend @('--json', 'share-status')
  Apply-ShareState $status
  Append-Log "共享状态已刷新：$(Get-ShareStatusText $status)"
}

function Apply-State {
  param($Status)

  $script:LatestState = $Status
  $modelMovable = Format-NullableCount $Status.model_movable_threads

  $mainStatusLabel.Text = Get-FriendlyStatus $Status
  $statusHintLabel.Text = Get-PrimaryHint $Status
  $restoreStatusLabel.Text = Get-FriendlyStatus $Status
  $providerLabel.Text = "provider（提供方）: $($Status.current_provider)"
  $modelLabel.Text = if ($Status.current_model) { "model（模型）: $($Status.current_model)" } else { 'model（模型）: 未读取到' }
  $summaryLabel.Text = "历史记录 $($Status.total_threads) 条    会话文件 $($Status.session_file_count) 个    侧边栏索引 $($Status.indexed_threads) 条"
  $repairLabel.Text = "待处理 $($Status.movable_threads)    归属 $($Status.movable_database_threads)    模型 $modelMovable    会话 $($Status.movable_session_threads)    索引 $($Status.missing_session_index_entries)"
  $visibilityLabel.Text = "可见性：路径前缀 $($Status.cwd_prefix_threads)    用户消息标记 $($Status.missing_user_event_threads)    已归档 $($Status.archived_threads)"
  $pathLabel.Text = "Codex 数据位置: $($Status.codex_home)"

  $providersView.Items.Clear()
  foreach ($row in $Status.provider_counts) {
    $isCurrent = if ($row.provider -eq $Status.current_provider) { '当前' } else { '' }
    $item = New-Object System.Windows.Forms.ListViewItem([string]$row.provider)
    [void]$item.SubItems.Add([string]$row.count)
    [void]$item.SubItems.Add('数据库')
    [void]$item.SubItems.Add($isCurrent)
    [void]$providersView.Items.Add($item)
  }
  foreach ($row in $Status.session_provider_counts) {
    $isCurrent = if ($row.provider -eq $Status.current_provider) { '当前' } else { '' }
    $item = New-Object System.Windows.Forms.ListViewItem([string]$row.provider)
    [void]$item.SubItems.Add([string]$row.count)
    [void]$item.SubItems.Add('会话文件')
    [void]$item.SubItems.Add($isCurrent)
    [void]$providersView.Items.Add($item)
  }

  $backupList.Items.Clear()
  $script:BackupMap = @{}
  foreach ($backup in $Status.backups) {
    $label = "$($backup.modified_at)    $($backup.name)"
    $script:BackupMap[$label] = $backup.path
    [void]$backupList.Items.Add($label)
  }

  Update-ActionAvailability
}

function Apply-ShareState {
  param($Status)

  $script:ShareState = $Status
  $shareStatusLabel.Text = Get-ShareStatusText $Status
  $shareLogLabel.Text = "日志位置: $($Status.log_path)"
  $shareStartupLabel.Text = "开机入口: $($Status.startup_path)"

  if ($Status.enabled -and $Status.running) {
    $shareHintLabel.Text = '切换账号、API 或 model（模型）后，后台会自动把本机历史保持在当前可见位置。'
  } elseif ($Status.enabled) {
    $shareHintLabel.Text = '已设置开机启动，但当前没有检测到后台进程。可以点“同步一次”或重新开启。'
  } else {
    $shareHintLabel.Text = '未开启共享模式。经常切换账号/API 时建议开启。'
  }

  Update-ActionAvailability
}

function Confirm-Action {
  param(
    [string]$Message,
    [string]$Title = '确认操作'
  )

  $choice = [System.Windows.Forms.MessageBox]::Show(
    $Message,
    $Title,
    [System.Windows.Forms.MessageBoxButtons]::OKCancel,
    [System.Windows.Forms.MessageBoxIcon]::Question
  )

  return $choice -eq [System.Windows.Forms.DialogResult]::OK
}

function Test-CodexHomeDirectory {
  param([string]$Path)

  if (-not $Path -or -not (Test-Path -LiteralPath $Path -PathType Container)) {
    return $false
  }

  $configPath = Join-Path $Path 'config.toml'
  $dbPath = Join-Path $Path 'state_5.sqlite'
  return (Test-Path -LiteralPath $configPath -PathType Leaf) -and (Test-Path -LiteralPath $dbPath -PathType Leaf)
}

function Resolve-CodexHomeInput {
  param([string]$RawPath)

  if (-not $RawPath) {
    return $null
  }

  $expanded = [Environment]::ExpandEnvironmentVariables($RawPath.Trim().Trim('"'))
  if (-not $expanded) {
    return $null
  }

  $candidatePaths = @($expanded)
  try {
    $candidatePaths += (Join-Path $expanded '.codex')
  } catch {
  }

  foreach ($candidate in $candidatePaths) {
    if (Test-CodexHomeDirectory -Path $candidate) {
      return (Resolve-Path -LiteralPath $candidate).Path
    }
  }

  return $null
}

function Should-PromptForCodexHome {
  param([string]$Message)

  return $Message -match '找不到 Codex 数据目录|请选择 Codex 数据目录|config\.toml|state_5\.sqlite|Missing config file|Missing database file'
}

function Request-CodexHomeDirectory {
  param([string]$Reason = '')

  $defaultPath = if ($script:CodexHomeOverride) {
    $script:CodexHomeOverride
  } elseif ($env:USERPROFILE) {
    Join-Path $env:USERPROFILE '.codex'
  } else {
    ''
  }

  while ($true) {
    $message = "默认目录没有找到可用的 Codex 数据。请输入 Codex 数据目录。`r`n`r`n可以输入 .codex 目录，也可以输入它的上一层目录。"
    if ($Reason) {
      $message += "`r`n`r`n当前提示：$Reason"
    }

    $inputPath = [Microsoft.VisualBasic.Interaction]::InputBox(
      $message,
      '选择 Codex 数据目录',
      $defaultPath
    )

    if (-not $inputPath) {
      return $false
    }

    $resolvedPath = Resolve-CodexHomeInput -RawPath $inputPath
    if ($resolvedPath) {
      $script:CodexHomeOverride = $resolvedPath
      $script:LatestState = $null
      $script:ShareState = $null
      $script:ManualBackupDone = $false
      return $true
    }

    $retry = [System.Windows.Forms.MessageBox]::Show(
      "这个目录里没有找到 config.toml 和 state_5.sqlite。`r`n`r`n请确认选择的是 Codex 数据目录，是否重新输入？",
      '目录不正确',
      [System.Windows.Forms.MessageBoxButtons]::YesNo,
      [System.Windows.Forms.MessageBoxIcon]::Warning
    )

    if ($retry -ne [System.Windows.Forms.DialogResult]::Yes) {
      return $false
    }
  }
}

function Open-PathInExplorer {
  param(
    [string]$Path,
    [switch]$EnsureFolder
  )

  if ($EnsureFolder -and -not (Test-Path -LiteralPath $Path)) {
    New-Item -ItemType Directory -Force -Path $Path | Out-Null
  }

  if (-not (Test-Path -LiteralPath $Path)) {
    throw "路径不存在: $Path"
  }

  Start-Process -FilePath explorer.exe -ArgumentList "`"$Path`""
}

$form = New-Object System.Windows.Forms.Form
$form.Text = 'Codex 对话同步工具'
$form.StartPosition = 'CenterScreen'
$form.Size = New-Object System.Drawing.Size(1060, 700)
$form.MinimumSize = New-Object System.Drawing.Size(1060, 700)
$form.BackColor = $colorSurface
$form.Font = New-Object System.Drawing.Font('Microsoft YaHei UI', 9)
Set-AppIcon $form

$toolTip = New-Object System.Windows.Forms.ToolTip
$toolTip.InitialDelay = 250
$toolTip.ReshowDelay = 100
$toolTip.AutoPopDelay = 7000

$headerPanel = New-Object System.Windows.Forms.Panel
$headerPanel.Location = New-Object System.Drawing.Point(0, 0)
$headerPanel.Size = New-Object System.Drawing.Size(1060, 80)
$headerPanel.Anchor = 'Top,Left,Right'
$headerPanel.BackColor = $colorPanel
$form.Controls.Add($headerPanel)

$headerLabel = New-Label -Text 'Codex 对话同步工具' -X 28 -Y 22 -Width 420 -Height 32 -Size 18 -Color $colorText -Style ([System.Drawing.FontStyle]::Bold)
$headerPanel.Controls.Add($headerLabel)

$headerDivider = New-Object System.Windows.Forms.Panel
$headerDivider.Location = New-Object System.Drawing.Point(0, 79)
$headerDivider.Size = New-Object System.Drawing.Size(1060, 1)
$headerDivider.Anchor = 'Left,Right,Top'
$headerDivider.BackColor = $colorBorder
$headerPanel.Controls.Add($headerDivider)

$statusPanel = New-Panel -X 28 -Y 96 -Width 996 -Height 104 -BackColor $colorSoftWarm
$statusPanel.Anchor = 'Top,Left,Right'
$form.Controls.Add($statusPanel)

$backupNoticeLabel = New-Label -Text '一定要开启手动备份。备份完成后，再开始后面的操作。' -X 18 -Y 10 -Width 760 -Height 24 -Size 11 -Color $colorText -Style ([System.Drawing.FontStyle]::Bold)
$statusPanel.Controls.Add($backupNoticeLabel)

$mainStatusLabel = New-Label -Text '正在读取 Codex 状态...' -X 18 -Y 40 -Width 760 -Height 26 -Size 12 -Color $colorText -Style ([System.Drawing.FontStyle]::Bold)
$statusPanel.Controls.Add($mainStatusLabel)

$statusHintLabel = New-Label -Text '默认先查找本机 Codex 数据目录；如果找不到，会提示你手动输入目录。' -X 18 -Y 70 -Width 760 -Height 20 -Size 9 -Color $colorMuted
$statusPanel.Controls.Add($statusHintLabel)

$safetyBadge = New-Label -Text '先手动备份' -X 812 -Y 36 -Width 154 -Height 28 -Size 9 -Color $colorAccent -Style ([System.Drawing.FontStyle]::Bold) -Align ([System.Drawing.ContentAlignment]::MiddleCenter)
$safetyBadge.BackColor = $colorSoftTeal
$statusPanel.Controls.Add($safetyBadge)

$progressBar = New-Object System.Windows.Forms.ProgressBar
$progressBar.Location = New-Object System.Drawing.Point(28, 208)
$progressBar.Size = New-Object System.Drawing.Size(996, 6)
$progressBar.Anchor = 'Top,Left,Right'
$progressBar.Visible = $false
$form.Controls.Add($progressBar)

$workspacePanel = New-Object System.Windows.Forms.Panel
$workspacePanel.Location = New-Object System.Drawing.Point(28, 228)
$workspacePanel.Size = New-Object System.Drawing.Size(996, 380)
$workspacePanel.Anchor = 'Top,Left,Right'
$workspacePanel.BackColor = $colorSurface
$form.Controls.Add($workspacePanel)

$sidePanel = New-Panel -X 0 -Y 0 -Width 178 -Height 380 -BackColor $colorPanel
$workspacePanel.Controls.Add($sidePanel)

$modeTitle = New-Label -Text '模式' -X 16 -Y 16 -Width 140 -Height 22 -Size 10 -Color $colorText -Style ([System.Drawing.FontStyle]::Bold)
$sidePanel.Controls.Add($modeTitle)

$restoreModeButton = New-Button -Text "恢复模式`r`n找回记录" -X 16 -Y 54 -Width 146 -Height 54 -Kind Primary
$restoreModeButton.Font = New-Object System.Drawing.Font('Microsoft YaHei UI', 9, [System.Drawing.FontStyle]::Bold)
$sidePanel.Controls.Add($restoreModeButton)

$shareModeButton = New-Button -Text "共享模式`r`n持续保持" -X 16 -Y 120 -Width 146 -Height 54
$shareModeButton.Font = New-Object System.Drawing.Font('Microsoft YaHei UI', 9, [System.Drawing.FontStyle]::Bold)
$sidePanel.Controls.Add($shareModeButton)

$sideNote = New-Label -Text '恢复模式适合手动修一次；共享模式适合经常切换账号、API 或 model（模型）。' -X 16 -Y 198 -Width 146 -Height 92 -Size 9 -Color $colorMuted
$sideNote.AutoEllipsis = $false
$sidePanel.Controls.Add($sideNote)

$sideFooter = New-Label -Text '不会删除你的聊天记录。' -X 16 -Y 320 -Width 146 -Height 22 -Size 9 -Color $colorAccent -Style ([System.Drawing.FontStyle]::Bold)
$sidePanel.Controls.Add($sideFooter)

$contentPanel = New-Object System.Windows.Forms.Panel
$contentPanel.Location = New-Object System.Drawing.Point(196, 0)
$contentPanel.Size = New-Object System.Drawing.Size(800, 380)
$contentPanel.Anchor = 'Top,Left,Right'
$contentPanel.BackColor = $colorSurface
$workspacePanel.Controls.Add($contentPanel)

$restorePage = New-Object System.Windows.Forms.Panel
$restorePage.Location = New-Object System.Drawing.Point(0, 0)
$restorePage.Size = New-Object System.Drawing.Size(800, 380)
$restorePage.Anchor = 'Top,Left,Right'
$restorePage.BackColor = $colorSurface
$contentPanel.Controls.Add($restorePage)

$sharePage = New-Object System.Windows.Forms.Panel
$sharePage.Location = New-Object System.Drawing.Point(0, 0)
$sharePage.Size = New-Object System.Drawing.Size(800, 380)
$sharePage.Anchor = 'Top,Left,Right'
$sharePage.BackColor = $colorSurface
$sharePage.Visible = $false
$contentPanel.Controls.Add($sharePage)

$restoreTitle = New-Label -Text '恢复模式' -X 0 -Y 2 -Width 180 -Height 30 -Size 14 -Color $colorText -Style ([System.Drawing.FontStyle]::Bold)
$restorePage.Controls.Add($restoreTitle)
$restoreLead = New-Label -Text '适合聊天记录已经看不见时，一次性修复 provider（提供方）、model（模型）、会话文件和侧边栏索引。' -X 0 -Y 36 -Width 790 -Height 22 -Size 9 -Color $colorMuted
$restorePage.Controls.Add($restoreLead)

$restoreStatePanel = New-Panel -X 0 -Y 70 -Width 386 -Height 118 -BackColor $colorPanel
$restorePage.Controls.Add($restoreStatePanel)
$restoreStatusLabel = New-Label -Text '正在读取状态...' -X 16 -Y 12 -Width 350 -Height 24 -Size 10 -Color $colorText -Style ([System.Drawing.FontStyle]::Bold)
$restoreStatePanel.Controls.Add($restoreStatusLabel)
$providerLabel = New-Label -Text 'provider（提供方）:' -X 16 -Y 42 -Width 350 -Height 21 -Color $colorMuted
$restoreStatePanel.Controls.Add($providerLabel)
$modelLabel = New-Label -Text 'model（模型）:' -X 16 -Y 66 -Width 350 -Height 21 -Color $colorMuted
$restoreStatePanel.Controls.Add($modelLabel)
$summaryLabel = New-Label -Text '历史记录' -X 16 -Y 90 -Width 350 -Height 21 -Color $colorMuted
$restoreStatePanel.Controls.Add($summaryLabel)

$restoreRepairPanel = New-Panel -X 404 -Y 70 -Width 396 -Height 118 -BackColor $colorPanel
$restorePage.Controls.Add($restoreRepairPanel)
$repairTitle = New-Label -Text '需要处理' -X 16 -Y 12 -Width 360 -Height 24 -Size 10 -Color $colorText -Style ([System.Drawing.FontStyle]::Bold)
$restoreRepairPanel.Controls.Add($repairTitle)
$repairLabel = New-Label -Text '待处理' -X 16 -Y 42 -Width 360 -Height 21 -Color $colorMuted
$restoreRepairPanel.Controls.Add($repairLabel)
$visibilityLabel = New-Label -Text '可见性' -X 16 -Y 66 -Width 360 -Height 21 -Color $colorMuted
$restoreRepairPanel.Controls.Add($visibilityLabel)
$pathLabel = New-Label -Text 'Codex 数据位置:' -X 16 -Y 90 -Width 360 -Height 21 -Color $colorMuted
$restoreRepairPanel.Controls.Add($pathLabel)

$actionLabel = New-Label -Text '操作' -X 0 -Y 204 -Width 120 -Height 20 -Size 10 -Color $colorText -Style ([System.Drawing.FontStyle]::Bold)
$restorePage.Controls.Add($actionLabel)

$syncButton = New-Button -Text '一键恢复' -X 0 -Y 230 -Width 104 -Height 38 -Kind Primary
$restorePage.Controls.Add($syncButton)
$refreshButton = New-Button -Text '重新检查' -X 112 -Y 230 -Width 88 -Height 38
$restorePage.Controls.Add($refreshButton)
$chooseCodexFolderButton = New-Button -Text '选择目录' -X 208 -Y 230 -Width 88 -Height 38
$restorePage.Controls.Add($chooseCodexFolderButton)
$backupButton = New-Button -Text '手动备份' -X 304 -Y 230 -Width 88 -Height 38 -Kind Accent
$restorePage.Controls.Add($backupButton)
$openCodexFolderButton = New-Button -Text '数据目录' -X 400 -Y 230 -Width 82 -Height 38
$restorePage.Controls.Add($openCodexFolderButton)
$openBackupsButton = New-Button -Text '备份目录' -X 490 -Y 230 -Width 82 -Height 38
$restorePage.Controls.Add($openBackupsButton)
$shortcutButton = New-Button -Text '桌面入口' -X 580 -Y 230 -Width 90 -Height 38
$restorePage.Controls.Add($shortcutButton)

$restoreButton = New-Button -Text '恢复选中' -X 684 -Y 212 -Width 116 -Height 36 -Kind Danger
$restorePage.Controls.Add($restoreButton)
$restoreLatestButton = New-Button -Text '恢复最新' -X 684 -Y 254 -Width 116 -Height 36 -Kind Danger
$restorePage.Controls.Add($restoreLatestButton)

$historyLabel = New-Label -Text '历史分布' -X 0 -Y 292 -Width 140 -Height 18 -Size 9 -Color $colorText -Style ([System.Drawing.FontStyle]::Bold)
$restorePage.Controls.Add($historyLabel)
$backupLabel = New-Label -Text '最近备份' -X 404 -Y 292 -Width 140 -Height 18 -Size 9 -Color $colorText -Style ([System.Drawing.FontStyle]::Bold)
$restorePage.Controls.Add($backupLabel)

$providersView = New-Object System.Windows.Forms.ListView
$providersView.View = 'Details'
$providersView.FullRowSelect = $true
$providersView.GridLines = $false
$providersView.Location = New-Object System.Drawing.Point(0, 314)
$providersView.Size = New-Object System.Drawing.Size(386, 58)
$providersView.BackColor = $colorPanel
$providersView.BorderStyle = 'FixedSingle'
[void]$providersView.Columns.Add('provider（提供方）', 148)
[void]$providersView.Columns.Add('数量', 62)
[void]$providersView.Columns.Add('来源', 100)
[void]$providersView.Columns.Add('状态', 64)
$restorePage.Controls.Add($providersView)

$backupList = New-Object System.Windows.Forms.ListBox
$backupList.Location = New-Object System.Drawing.Point(404, 314)
$backupList.Size = New-Object System.Drawing.Size(396, 58)
$backupList.BackColor = $colorPanel
$backupList.BorderStyle = 'FixedSingle'
$restorePage.Controls.Add($backupList)

$shareTitle = New-Label -Text '共享模式' -X 0 -Y 2 -Width 180 -Height 30 -Size 14 -Color $colorText -Style ([System.Drawing.FontStyle]::Bold)
$sharePage.Controls.Add($shareTitle)
$shareLead = New-Label -Text '适合长期在不同账号、API、provider（提供方）或 model（模型）之间切换。' -X 0 -Y 36 -Width 790 -Height 22 -Size 9 -Color $colorMuted
$sharePage.Controls.Add($shareLead)

$shareStatePanel = New-Panel -X 0 -Y 70 -Width 800 -Height 126 -BackColor $colorPanel
$sharePage.Controls.Add($shareStatePanel)
$shareStatusLabel = New-Label -Text '共享模式未开启' -X 18 -Y 14 -Width 748 -Height 28 -Size 12 -Color $colorText -Style ([System.Drawing.FontStyle]::Bold)
$shareStatePanel.Controls.Add($shareStatusLabel)
$shareHintLabel = New-Label -Text '经常切换账号/API 时建议开启共享模式。' -X 18 -Y 48 -Width 748 -Height 22 -Color $colorMuted
$shareStatePanel.Controls.Add($shareHintLabel)
$shareLogLabel = New-Label -Text '日志位置:' -X 18 -Y 74 -Width 748 -Height 20 -Color $colorMuted
$shareStatePanel.Controls.Add($shareLogLabel)
$shareStartupLabel = New-Label -Text '开机入口:' -X 18 -Y 96 -Width 748 -Height 20 -Color $colorMuted
$shareStatePanel.Controls.Add($shareStartupLabel)

$shareActionLabel = New-Label -Text '操作' -X 0 -Y 216 -Width 120 -Height 20 -Size 10 -Color $colorText -Style ([System.Drawing.FontStyle]::Bold)
$sharePage.Controls.Add($shareActionLabel)

$shareEnableButton = New-Button -Text '开启共享模式' -X 0 -Y 242 -Width 132 -Height 38 -Kind Primary
$sharePage.Controls.Add($shareEnableButton)
$shareDisableButton = New-Button -Text '关闭共享模式' -X 144 -Y 242 -Width 132 -Height 38 -Kind Danger
$sharePage.Controls.Add($shareDisableButton)
$shareOnceButton = New-Button -Text '同步一次' -X 288 -Y 242 -Width 104 -Height 38 -Kind Accent
$sharePage.Controls.Add($shareOnceButton)
$shareRefreshButton = New-Button -Text '刷新状态' -X 404 -Y 242 -Width 104 -Height 38
$sharePage.Controls.Add($shareRefreshButton)
$openShareLogButton = New-Button -Text '打开日志' -X 520 -Y 242 -Width 104 -Height 38
$sharePage.Controls.Add($openShareLogButton)

$shareModeNote = New-Panel -X 0 -Y 306 -Width 386 -Height 66 -BackColor $colorPanel
$sharePage.Controls.Add($shareModeNote)
$shareModeNoteText = New-Label -Text '后台会跟随当前 Codex 设置，持续修复数据库、会话文件、侧边栏索引和可见性标记。' -X 16 -Y 12 -Width 350 -Height 42 -Color $colorMuted
$shareModeNoteText.AutoEllipsis = $false
$shareModeNote.Controls.Add($shareModeNoteText)

$shareSafeNote = New-Panel -X 404 -Y 306 -Width 396 -Height 66 -BackColor $colorPanel
$sharePage.Controls.Add($shareSafeNote)
$shareSafeNoteText = New-Label -Text '有变更会自动备份；关闭共享模式会移除开机入口，并停止本工具记录的后台进程。' -X 16 -Y 12 -Width 360 -Height 42 -Color $colorMuted
$shareSafeNoteText.AutoEllipsis = $false
$shareSafeNote.Controls.Add($shareSafeNoteText)

function Set-ModeButtonVisual {
  param(
    [System.Windows.Forms.Button]$Button,
    [bool]$Active
  )

  if ($Active) {
    $Button.BackColor = $colorPrimary
    $Button.ForeColor = [System.Drawing.Color]::White
    $Button.FlatAppearance.BorderColor = $colorPrimary
    $Button.FlatAppearance.MouseOverBackColor = $colorPrimaryHover
    $Button.FlatAppearance.MouseDownBackColor = $colorPrimaryHover
  } else {
    $Button.BackColor = $colorPanel
    $Button.ForeColor = $colorText
    $Button.FlatAppearance.BorderColor = $colorBorder
    $Button.FlatAppearance.MouseOverBackColor = $colorSoftInfo
    $Button.FlatAppearance.MouseDownBackColor = $colorSoftTeal
  }
}

function Show-Mode {
  param(
    [ValidateSet('Restore', 'Share')]
    [string]$Mode
  )

  $restoreActive = $Mode -eq 'Restore'
  $restorePage.Visible = $restoreActive
  $sharePage.Visible = -not $restoreActive
  Set-ModeButtonVisual -Button $restoreModeButton -Active $restoreActive
  Set-ModeButtonVisual -Button $shareModeButton -Active (-not $restoreActive)
}

$restoreModeButton.Add_Click({ Show-Mode -Mode 'Restore' })
$shareModeButton.Add_Click({ Show-Mode -Mode 'Share' })
Show-Mode -Mode 'Restore'

Set-ControlTip $restoreModeButton '查看恢复模式。'
Set-ControlTip $shareModeButton '查看共享模式。'
Set-ControlTip $syncButton '把看不见的本机历史记录恢复到当前 Codex 设置下。'
Set-ControlTip $refreshButton '重新读取 Codex 数据库、会话文件和共享模式状态。'
Set-ControlTip $chooseCodexFolderButton '默认目录找不到时，手动输入这台电脑上的 Codex 数据目录。'
Set-ControlTip $backupButton '手动保存一次当前 Codex 历史状态；完成后才能继续恢复或共享。'
Set-ControlTip $openCodexFolderButton '打开本机 Codex 数据目录。'
Set-ControlTip $openBackupsButton '打开本工具保存的备份目录。'
Set-ControlTip $shortcutButton '重建桌面快捷方式，并使用项目图标。'
Set-ControlTip $restoreButton '从选中的备份恢复，恢复前会再保存当前状态。'
Set-ControlTip $restoreLatestButton '从最近一次备份恢复，恢复前会再保存当前状态。'
Set-ControlTip $shareEnableButton '开启后台共享模式，并创建开机启动入口。'
Set-ControlTip $shareDisableButton '关闭后台共享模式，并移除开机启动入口。'
Set-ControlTip $shareOnceButton '不常驻后台，只立即同步一次当前状态。'
Set-ControlTip $shareRefreshButton '重新读取共享模式是否开启、后台是否运行。'
Set-ControlTip $openShareLogButton '打开共享模式日志文件所在位置。'

$backupList.Add_SelectedIndexChanged({
  Update-ActionAvailability
})

$chooseCodexFolderButton.Add_Click({
  try {
    if (Request-CodexHomeDirectory -Reason '请为这台电脑选择 Codex 数据目录。') {
      Set-Busy -Busy $true -Message '正在读取所选目录...'
      Refresh-State
      Refresh-ShareState
      [System.Windows.Forms.MessageBox]::Show("已使用这个 Codex 数据目录：`r`n$script:CodexHomeOverride`r`n`r`n请先完成手动备份，再继续操作。", '目录已设置', 'OK', 'Information') | Out-Null
    }
  } catch {
    [System.Windows.Forms.MessageBox]::Show($_.Exception.Message, '目录设置失败', 'OK', 'Error') | Out-Null
    Append-Log "目录设置失败: $($_.Exception.Message)"
  } finally {
    Set-Busy -Busy $false
  }
})

$refreshButton.Add_Click({
  try {
    Set-Busy -Busy $true -Message '正在重新检查 Codex 状态...'
    Refresh-State
    Refresh-ShareState
    [System.Windows.Forms.MessageBox]::Show('检查完成。请先确认已经手动备份，再继续操作。', '检查完成', 'OK', 'Information') | Out-Null
  } catch {
    [System.Windows.Forms.MessageBox]::Show($_.Exception.Message, '检查失败', 'OK', 'Error') | Out-Null
    Append-Log "检查失败: $($_.Exception.Message)"
  } finally {
    Set-Busy -Busy $false
  }
})

$syncButton.Add_Click({
  try {
    if (-not $script:LatestState) {
      Refresh-State
    }
    if ([int]$script:LatestState.movable_threads -le 0) {
      [System.Windows.Forms.MessageBox]::Show('当前已经整理好了，不需要恢复。', '无需恢复', 'OK', 'Information') | Out-Null
      Append-Log '恢复跳过：没有需要处理的历史。'
      return
    }

    $message = "将把本机聊天记录挂回当前 Codex 可见位置，并补齐侧边栏索引与可见性标记。`r`n`r`nprovider（提供方）: $($script:LatestState.current_provider)`r`nmodel（模型）: $($script:LatestState.current_model)`r`n待处理: $($script:LatestState.movable_threads)`r`n`r`n你已经完成手动备份。本次恢复还会自动保存安全备份。"
    if (-not (Confirm-Action -Message $message -Title '开始一键恢复？')) {
      Append-Log '用户取消了恢复。'
      return
    }

    Set-Busy -Busy $true -Message '正在恢复聊天记录...'
    $result = Invoke-Backend @('--json', 'sync', '--passes', '3')
    Append-Log "恢复完成：数据库 $($result.updated_rows) 条，会话文件 $($result.updated_session_files) 个，执行轮次 $($result.passes)。"
    Append-Log "可见性修复：路径 $($result.visibility_updates.normalized_cwd)，用户消息标记 $($result.visibility_updates.set_has_user_event)，取消归档 $($result.visibility_updates.unarchived)。"
    Append-Log "侧边栏索引已重建 $($result.rewritten_index_entries) 条，补回 $($result.missing_session_index_entries_before) 条。"
    Append-Log "耗时: $(Format-Duration $result.timing.total_ms)，备份: $($result.backup_path)"
    Append-Log "provider（提供方）同步前: $(Format-Counts $result.before_counts)"
    Append-Log "provider（提供方）同步后: $(Format-Counts $result.after_counts)"
    Append-Log "model（模型）同步前: $(Format-ModelCounts $result.before_model_counts)"
    Append-Log "model（模型）同步后: $(Format-ModelCounts $result.after_model_counts)"
    Apply-State $result.status
    [System.Windows.Forms.MessageBox]::Show('恢复完成。如果 Codex 侧边栏没有马上刷新，请重启 Codex Desktop。', '恢复完成', 'OK', 'Information') | Out-Null
  } catch {
    [System.Windows.Forms.MessageBox]::Show($_.Exception.Message, '恢复失败', 'OK', 'Error') | Out-Null
    Append-Log "恢复失败: $($_.Exception.Message)"
  } finally {
    Set-Busy -Busy $false
  }
})

$backupButton.Add_Click({
  try {
    Set-Busy -Busy $true -Message '正在创建备份...'
    $result = Invoke-Backend @('--json', 'backup')
    $script:ManualBackupDone = $true
    Append-Log "备份完成: $($result.backup_path)"
    Append-Log "备份耗时: $(Format-Duration $result.timing.total_ms)"
    Refresh-State
    [System.Windows.Forms.MessageBox]::Show("手动备份完成。现在可以继续恢复、还原或共享操作。`r`n`r`n备份位置：`r`n$($result.backup_path)", '备份完成', 'OK', 'Information') | Out-Null
  } catch {
    [System.Windows.Forms.MessageBox]::Show($_.Exception.Message, '备份失败', 'OK', 'Error') | Out-Null
    Append-Log "备份失败: $($_.Exception.Message)"
  } finally {
    Set-Busy -Busy $false
  }
})

$openCodexFolderButton.Add_Click({
  try {
    if (-not $script:LatestState) {
      Refresh-State
    }
    Open-PathInExplorer -Path ([string]$script:LatestState.codex_home) -EnsureFolder
    Append-Log "已打开 Codex 数据目录: $($script:LatestState.codex_home)"
  } catch {
    [System.Windows.Forms.MessageBox]::Show($_.Exception.Message, '打开目录失败', 'OK', 'Error') | Out-Null
    Append-Log "打开 Codex 数据目录失败: $($_.Exception.Message)"
  }
})

$openBackupsButton.Add_Click({
  try {
    if (-not $script:LatestState) {
      Refresh-State
    }
    $folder = $script:LatestState.backup_dir
    Open-PathInExplorer -Path ([string]$folder) -EnsureFolder
    Append-Log "已打开备份目录: $folder"
  } catch {
    [System.Windows.Forms.MessageBox]::Show($_.Exception.Message, '打开目录失败', 'OK', 'Error') | Out-Null
    Append-Log "打开备份目录失败: $($_.Exception.Message)"
  }
})

$shortcutButton.Add_Click({
  try {
    $path = New-DesktopShortcut
    Append-Log "桌面入口已更新: $path"
    [System.Windows.Forms.MessageBox]::Show("桌面入口已更新：`r`n$path`r`n`r`n新图标会在 Windows 刷新图标缓存后显示。", '完成', 'OK', 'Information') | Out-Null
  } catch {
    [System.Windows.Forms.MessageBox]::Show($_.Exception.Message, '创建入口失败', 'OK', 'Error') | Out-Null
    Append-Log "创建入口失败: $($_.Exception.Message)"
  }
})

$restoreButton.Add_Click({
  try {
    if ($backupList.SelectedItem -eq $null) {
      [System.Windows.Forms.MessageBox]::Show('请先选一个备份。', '未选择备份', 'OK', 'Warning') | Out-Null
      return
    }
    $selectedLabel = [string]$backupList.SelectedItem
    $backupPath = $script:BackupMap[$selectedLabel]
    if (-not $backupPath) {
      throw '无法解析选中的备份路径。'
    }

    if (-not (Confirm-Action -Message "将恢复这个备份：`r`n$backupPath`r`n`r`n恢复前会再保存当前状态，方便回到恢复前。" -Title '确认恢复选中备份？')) {
      Append-Log '用户取消了备份恢复。'
      return
    }

    Set-Busy -Busy $true -Message '正在恢复选中备份...'
    $result = Invoke-Backend @('--json', 'restore', '--backup', $backupPath)
    Append-Log "备份恢复完成。来源: $($result.restored_from)"
    Append-Log "恢复前安全备份: $($result.safety_backup)"
    Append-Log "恢复耗时: $(Format-Duration $result.timing.total_ms)"
    Apply-State $result.status
    [System.Windows.Forms.MessageBox]::Show("备份恢复完成。`r`n`r`n来源：`r`n$($result.restored_from)", '恢复完成', 'OK', 'Information') | Out-Null
  } catch {
    [System.Windows.Forms.MessageBox]::Show($_.Exception.Message, '恢复失败', 'OK', 'Error') | Out-Null
    Append-Log "恢复失败: $($_.Exception.Message)"
  } finally {
    Set-Busy -Busy $false
  }
})

$restoreLatestButton.Add_Click({
  try {
    if (-not (Confirm-Action -Message '将恢复最近一次备份，并在恢复前保存当前状态。' -Title '确认恢复最新备份？')) {
      Append-Log '用户取消了恢复最新备份。'
      return
    }

    Set-Busy -Busy $true -Message '正在恢复最新备份...'
    $result = Invoke-Backend @('--json', 'restore')
    Append-Log "已恢复最新备份: $($result.restored_from)"
    Append-Log "恢复前安全备份: $($result.safety_backup)"
    Append-Log "恢复耗时: $(Format-Duration $result.timing.total_ms)"
    Apply-State $result.status
    [System.Windows.Forms.MessageBox]::Show("已恢复最新备份。`r`n`r`n来源：`r`n$($result.restored_from)", '恢复完成', 'OK', 'Information') | Out-Null
  } catch {
    [System.Windows.Forms.MessageBox]::Show($_.Exception.Message, '恢复失败', 'OK', 'Error') | Out-Null
    Append-Log "恢复失败: $($_.Exception.Message)"
  } finally {
    Set-Busy -Busy $false
  }
})

$shareRefreshButton.Add_Click({
  try {
    Set-Busy -Busy $true -Message '正在读取共享模式状态...'
    Refresh-ShareState
  } catch {
    [System.Windows.Forms.MessageBox]::Show($_.Exception.Message, '共享状态失败', 'OK', 'Error') | Out-Null
    Append-Log "共享状态失败: $($_.Exception.Message)"
  } finally {
    Set-Busy -Busy $false
  }
})

$shareEnableButton.Add_Click({
  try {
    if (-not (Confirm-Action -Message '将开启后台共享模式，并创建开机启动入口。`r`n`r`n开启后，切换账号/API/model（模型）时会持续保持本机聊天记录可见。' -Title '开启共享模式？')) {
      Append-Log '用户取消了开启共享模式。'
      return
    }
    Set-Busy -Busy $true -Message '正在开启共享模式...'
    $result = Invoke-Backend @('--json', 'share-enable', '--interval', '2')
    Apply-ShareState $result
    Append-Log "共享模式已开启：$(Get-ShareStatusText $result)"
    [System.Windows.Forms.MessageBox]::Show('共享模式已开启。之后切换账号、API 或模型时，工具会尽量保持本机聊天记录可见。', '开启完成', 'OK', 'Information') | Out-Null
  } catch {
    [System.Windows.Forms.MessageBox]::Show($_.Exception.Message, '开启失败', 'OK', 'Error') | Out-Null
    Append-Log "开启共享模式失败: $($_.Exception.Message)"
  } finally {
    Set-Busy -Busy $false
  }
})

$shareDisableButton.Add_Click({
  try {
    if (-not (Confirm-Action -Message '将关闭后台共享模式，并移除开机启动入口。`r`n`r`n已经保存的备份不会删除。' -Title '关闭共享模式？')) {
      Append-Log '用户取消了关闭共享模式。'
      return
    }
    Set-Busy -Busy $true -Message '正在关闭共享模式...'
    $result = Invoke-Backend @('--json', 'share-disable')
    Apply-ShareState $result
    Append-Log '共享模式已关闭。'
    [System.Windows.Forms.MessageBox]::Show('共享模式已关闭，开机入口已移除。', '关闭完成', 'OK', 'Information') | Out-Null
  } catch {
    [System.Windows.Forms.MessageBox]::Show($_.Exception.Message, '关闭失败', 'OK', 'Error') | Out-Null
    Append-Log "关闭共享模式失败: $($_.Exception.Message)"
  } finally {
    Set-Busy -Busy $false
  }
})

$shareOnceButton.Add_Click({
  try {
    Set-Busy -Busy $true -Message '正在同步一次共享状态...'
    $result = Invoke-Backend @('--json', 'share-once')
    if ($result.changed) {
      Append-Log "共享同步完成：数据库 $($result.updated_rows) 条，会话文件 $($result.updated_session_files) 个。"
      Append-Log "可见性修复：路径 $($result.visibility_updates.normalized_cwd)，用户消息标记 $($result.visibility_updates.set_has_user_event)，取消归档 $($result.visibility_updates.unarchived)。"
    } else {
      Append-Log '共享同步跳过：当前没有需要处理的历史。'
    }
    Apply-State $result.status
    Refresh-ShareState
    [System.Windows.Forms.MessageBox]::Show('共享同步完成。', '同步完成', 'OK', 'Information') | Out-Null
  } catch {
    [System.Windows.Forms.MessageBox]::Show($_.Exception.Message, '同步失败', 'OK', 'Error') | Out-Null
    Append-Log "共享同步失败: $($_.Exception.Message)"
  } finally {
    Set-Busy -Busy $false
  }
})

$openShareLogButton.Add_Click({
  try {
    if (-not $script:ShareState) {
      Refresh-ShareState
    }
    $logPath = [string]$script:ShareState.log_path
    $folder = Split-Path -Parent $logPath
    if (-not (Test-Path -LiteralPath $folder)) {
      New-Item -ItemType Directory -Force -Path $folder | Out-Null
    }
    if (-not (Test-Path -LiteralPath $logPath)) {
      New-Item -ItemType File -Force -Path $logPath | Out-Null
    }
    Open-PathInExplorer -Path $logPath
    Append-Log "已打开共享日志: $logPath"
  } catch {
    [System.Windows.Forms.MessageBox]::Show($_.Exception.Message, '打开日志失败', 'OK', 'Error') | Out-Null
    Append-Log "打开共享日志失败: $($_.Exception.Message)"
  }
})

Update-ActionAvailability

if ($SmokeTest) {
  Write-Output 'Smoke test OK'
  exit 0
}

try {
  $createdShortcut = New-DesktopShortcut
  Append-Log "桌面入口已准备好: $createdShortcut"
} catch {
  Append-Log "初始化桌面入口失败: $($_.Exception.Message)"
}

try {
  Refresh-State
  Refresh-ShareState
} catch {
  Append-Log "初始化状态失败: $($_.Exception.Message)"
  [System.Windows.Forms.MessageBox]::Show($_.Exception.Message, '启动失败', 'OK', 'Error') | Out-Null
}

[void]$form.ShowDialog()

