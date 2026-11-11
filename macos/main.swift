import AppKit

private let codexBundleID = "com.openai.codex"
private let guideURL = URL(string: "https://github.com/jcz111/codex-sidebar-recovery/blob/main/README.md#macos-窗口版")!

private struct SyncResult: Decodable {
    let applied: Bool
    let active_projects: Int
    let active_chats: Int
    let saved_projects: Int
    let saved_chats: Int
    let shared_projects: Int
    let skipped: [String: Int]
    let backup: String
}

private enum SyncError: LocalizedError {
    case pythonMissing, resourcesMissing, quitTimeout, scriptFailed(String)

    var errorDescription: String? {
        switch self {
        case .pythonMissing: return "需要 Python 3.10 或更新版本。"
        case .resourcesMissing: return "应用包缺少同步脚本。"
        case .quitTimeout: return "Codex 尚未完全退出。请处理客户端的退出提示，再重试。"
        case .scriptFailed(let message): return message
        }
    }
}

private func python() throws -> URL {
    let candidates = ["/opt/homebrew/bin/python3", "/usr/local/bin/python3", "/usr/bin/python3"]
    for path in candidates where FileManager.default.isExecutableFile(atPath: path) {
        let process = Process()
        process.executableURL = URL(fileURLWithPath: path)
        process.arguments = ["-c", "import sys; sys.exit(0 if sys.version_info >= (3, 10) else 1)"]
        process.standardOutput = FileHandle.nullDevice
        process.standardError = FileHandle.nullDevice
        do {
            try process.run()
            process.waitUntilExit()
            if process.terminationStatus == 0 { return URL(fileURLWithPath: path) }
        } catch {
            continue
        }
    }
    throw SyncError.pythonMissing
}

private func sync(using executable: URL, includeChats: Bool) throws -> SyncResult {
    guard let script = Bundle.main.url(forResource: "sync_sidebar", withExtension: "py") else {
        throw SyncError.resourcesMissing
    }
    let parent = FileManager.default.homeDirectoryForCurrentUser
        .appendingPathComponent("Library/Application Support/Codex Sidebar Sync/Backups")
    for attempt in 0..<5 {
        let process = Process()
        let output = Pipe()
        process.executableURL = executable
        process.arguments = ["-B", script.path, "--backup-parent", parent.path, "--apply"]
        if includeChats { process.arguments?.append("--include-chats") }
        process.standardOutput = output
        process.standardError = output
        try process.run()
        let data = output.fileHandleForReading.readDataToEndOfFile()
        process.waitUntilExit()
        if process.terminationStatus == 0 {
            return try JSONDecoder().decode(SyncResult.self, from: data)
        }
        let message = String(decoding: data, as: UTF8.self).trimmingCharacters(in: .whitespacesAndNewlines)
        if attempt < 4 && (message.contains("background processes") || message.contains("may still be in use")) {
            Thread.sleep(forTimeInterval: 0.8)
        } else if message.contains("Exactly two saved Codex accounts") {
            throw SyncError.scriptFailed("需要在这台 Mac 上保存恰好两个 Codex 登录账号。")
        } else if message.contains("background processes") || message.contains("may still be in use") {
            throw SyncError.scriptFailed("Codex 后台进程仍在运行。请退出相关 Codex CLI 或终端任务，再试一次。")
        } else if message.contains("Live state changed") || message.contains("sidebar changed") || message.contains("sidebar hash differs") {
            throw SyncError.scriptFailed("同步时检测到配置变化。请等切号完成后再试。")
        } else if message.contains("file, schema, or process check failed") {
            throw SyncError.scriptFailed("无法核对 Codex 状态。请检查当前版本和本机账号是否符合使用指南。")
        } else {
            throw SyncError.scriptFailed(message.isEmpty ? "同步未完成。" : message)
        }
    }
    throw SyncError.quitTimeout
}

private func label(_ text: String, size: CGFloat = 13, weight: NSFont.Weight = .regular,
                   color: NSColor = .labelColor) -> NSTextField {
    let field = NSTextField(wrappingLabelWithString: text)
    field.font = .systemFont(ofSize: size, weight: weight)
    field.textColor = color
    return field
}

private func column(_ views: [NSView], spacing: CGFloat) -> NSStackView {
    let stack = NSStackView(views: views)
    stack.orientation = .vertical
    stack.alignment = .leading
    stack.spacing = spacing
    return stack
}

private func card(_ view: NSView) -> NSView {
    let container = NSView()
    let box = NSBox()
    box.boxType = .custom
    box.borderWidth = 1
    box.borderColor = .separatorColor
    box.fillColor = .controlBackgroundColor
    box.cornerRadius = 12
    for subview in [box, view] {
        container.addSubview(subview)
        subview.translatesAutoresizingMaskIntoConstraints = false
    }
    NSLayoutConstraint.activate([
        box.leadingAnchor.constraint(equalTo: container.leadingAnchor),
        box.trailingAnchor.constraint(equalTo: container.trailingAnchor),
        box.topAnchor.constraint(equalTo: container.topAnchor),
        box.bottomAnchor.constraint(equalTo: container.bottomAnchor),
        view.leadingAnchor.constraint(equalTo: container.leadingAnchor, constant: 16),
        view.trailingAnchor.constraint(equalTo: container.trailingAnchor, constant: -16),
        view.topAnchor.constraint(equalTo: container.topAnchor, constant: 13),
        view.bottomAnchor.constraint(equalTo: container.bottomAnchor, constant: -13),
    ])
    return container
}

private final class App: NSObject, NSApplicationDelegate, NSWindowDelegate {
    private let window = NSWindow(contentRect: NSRect(x: 0, y: 0, width: 525, height: 432),
                                  styleMask: [.titled, .closable, .miniaturizable],
                                  backing: .buffered, defer: false)
    private let includeChats = NSButton(checkboxWithTitle: "同步会话归组", target: nil, action: nil)
    private let statusIcon = NSImageView()
    private let statusTitle = label("", size: 15, weight: .semibold)
    private let statusDetail = label("", color: .secondaryLabelColor)
    private let progress = NSProgressIndicator()
    private lazy var synchronize = NSButton(title: "退出 Codex 并同步", target: self, action: #selector(begin))
    private lazy var reveal = NSButton(title: "查看备份", target: self, action: #selector(showBackup))
    private lazy var guide = NSButton(title: "使用指南", target: self, action: #selector(openGuide))
    private lazy var reopen = NSButton(title: "打开 Codex", target: self, action: #selector(openCodex))
    private var backup: URL?
    private var codexURL: URL?
    private var busy = false

    func applicationDidFinishLaunching(_ notification: Notification) {
        let title = label("Codex 侧栏同步", size: 26, weight: .semibold)
        let subtitle = label("在同一台 Mac 上，补齐两个账号的本地项目和会话归组。", color: .secondaryLabelColor)
        let icon = NSImageView()
        icon.image = NSImage(systemSymbolName: "rectangle.split.2x1", accessibilityDescription: nil)
        icon.symbolConfiguration = .init(pointSize: 31, weight: .medium)
        icon.contentTintColor = .controlAccentColor
        icon.setAccessibilityElement(false)
        let header = NSStackView(views: [icon, column([title, subtitle], spacing: 4)])
        header.alignment = .centerY
        header.spacing = 14

        let scopeTitle = label("同步范围", size: 12, weight: .semibold, color: .secondaryLabelColor)
        let project = label("本地项目和排序", size: 15, weight: .semibold)
        let scopeDetail = label("依据两个账号已保存的项目补齐侧栏。", color: .secondaryLabelColor)
        let chatDetail = label("仅加入已核验的本机会话，保留已有归组。", color: .secondaryLabelColor)
        includeChats.state = .on
        let chatOptions = column([includeChats, chatDetail], spacing: 3)
        let scope = card(column([scopeTitle, column([project, scopeDetail], spacing: 3), chatOptions], spacing: 11))

        statusIcon.symbolConfiguration = .init(pointSize: 17, weight: .medium)
        statusIcon.setAccessibilityElement(false)
        progress.style = .spinning
        progress.controlSize = .small
        progress.isDisplayedWhenStopped = false
        progress.setAccessibilityLabel("同步进度")
        statusDetail.preferredMaxLayoutWidth = 375
        let statusRow = NSStackView(views: [statusIcon, column([statusTitle, statusDetail], spacing: 4), progress])
        statusRow.alignment = .top
        statusRow.spacing = 10
        let result = card(statusRow)

        for button in [guide, reveal, reopen, synchronize] { button.bezelStyle = .rounded }
        reveal.isEnabled = false
        reopen.isHidden = true
        synchronize.keyEquivalent = "\r"
        let actions = NSStackView(views: [guide, reveal, reopen, NSView(), synchronize])
        actions.spacing = 8
        actions.distribution = .fill
        let stack = column([header, scope, result, actions], spacing: 15)
        let content = NSView()
        content.addSubview(stack)
        stack.translatesAutoresizingMaskIntoConstraints = false
        NSLayoutConstraint.activate([
            stack.leadingAnchor.constraint(equalTo: content.leadingAnchor, constant: 24),
            stack.trailingAnchor.constraint(equalTo: content.trailingAnchor, constant: -24),
            stack.topAnchor.constraint(equalTo: content.topAnchor, constant: 23),
            stack.bottomAnchor.constraint(lessThanOrEqualTo: content.bottomAnchor, constant: -18),
            scope.widthAnchor.constraint(equalTo: stack.widthAnchor),
            result.widthAnchor.constraint(equalTo: stack.widthAnchor),
            actions.widthAnchor.constraint(equalTo: stack.widthAnchor),
        ])
        setStatus("准备就绪", detail: "切到要使用的账号，等登录完成后再同步。应用会先备份，完成后重新打开 Codex。",
                  symbol: "info.circle", tint: .secondaryLabelColor)
        window.title = "Codex 侧栏同步"
        window.delegate = self
        window.contentView = content
        window.center()
        window.makeKeyAndOrderFront(nil)
        NSApp.activate(ignoringOtherApps: true)
    }

    private func setStatus(_ title: String, detail: String, symbol: String, tint: NSColor) {
        statusTitle.stringValue = title
        statusDetail.stringValue = detail
        statusIcon.image = NSImage(systemSymbolName: symbol, accessibilityDescription: nil)
        statusIcon.contentTintColor = tint
    }

    func applicationShouldTerminateAfterLastWindowClosed(_ sender: NSApplication) -> Bool { true }
    func windowShouldClose(_ sender: NSWindow) -> Bool { !busy }

    func applicationShouldTerminate(_ sender: NSApplication) -> NSApplication.TerminateReply {
        busy ? .terminateCancel : .terminateNow
    }

    @objc private func begin() {
        busy = true
        synchronize.isEnabled = false
        includeChats.isEnabled = false
        reopen.isHidden = true
        progress.startAnimation(nil)
        let include = includeChats.state == .on
        setStatus("正在准备", detail: "正在检查本机的同步环境。", symbol: "arrow.triangle.2.circlepath",
                  tint: .controlAccentColor)
        DispatchQueue.global(qos: .userInitiated).async {
            let environment = Result { try python() }
            DispatchQueue.main.async {
                switch environment {
                case .success(let executable): self.quitCodex(using: executable, includeChats: include)
                case .failure(let error): self.finish(.failure(error))
                }
            }
        }
    }

    private func quitCodex(using executable: URL, includeChats: Bool) {
        let apps = NSRunningApplication.runningApplications(withBundleIdentifier: codexBundleID)
        codexURL = apps.first?.bundleURL ?? NSWorkspace.shared.urlForApplication(withBundleIdentifier: codexBundleID)
        if !apps.isEmpty {
            setStatus("正在等待 Codex 退出", detail: "如果 Codex 显示退出提示，请先在客户端处理。",
                      symbol: "hourglass", tint: .controlAccentColor)
            apps.forEach { _ = $0.terminate() }
        }
        waitForExit(apps: apps, executable: executable, includeChats: includeChats, attempts: 60)
    }

    private func waitForExit(apps: [NSRunningApplication], executable: URL, includeChats: Bool, attempts: Int) {
        if !apps.allSatisfy(\.isTerminated) {
            guard attempts > 0 else { finish(.failure(SyncError.quitTimeout)); return }
            DispatchQueue.main.asyncAfter(deadline: .now() + 0.25) {
                self.waitForExit(apps: apps, executable: executable, includeChats: includeChats, attempts: attempts - 1)
            }
            return
        }
        setStatus("正在备份并同步", detail: "正在核对本地项目、会话归组和配置。",
                  symbol: "externaldrive", tint: .controlAccentColor)
        DispatchQueue.global(qos: .userInitiated).async {
            let result = Result { try sync(using: executable, includeChats: includeChats) }
            DispatchQueue.main.async { self.finish(result) }
        }
    }

    private func finish(_ result: Result<SyncResult, Error>) {
        busy = false
        synchronize.isEnabled = true
        includeChats.isEnabled = true
        progress.stopAnimation(nil)
        switch result {
        case .success(let value):
            backup = URL(fileURLWithPath: value.backup)
            reveal.isEnabled = true
            let projects = value.active_projects + value.saved_projects
            let chats = value.active_chats + value.saved_chats
            let skipped = value.skipped.values.reduce(0, +)
            var detail = "两个账号共有 \(value.shared_projects) 个已核验项目。"
            if value.applied && projects == 0 && chats == 0 {
                detail = "已对齐项目排序。" + detail
            } else if value.applied {
                detail = "已补齐 \(projects) 个项目入口和 \(chats) 条会话归组。" + detail
            }
            if skipped > 0 { detail += " 跳过 \(skipped) 条记录，原因见备份。" }
            let title = value.applied ? "同步完成" : "可同步的项目已一致"
            setStatus(title, detail: detail, symbol: "checkmark.circle.fill", tint: .systemGreen)
            openCodex()
        case .failure(let error):
            reopen.isHidden = codexURL == nil
            setStatus("同步中止", detail: error.localizedDescription,
                      symbol: "exclamationmark.triangle.fill", tint: .systemOrange)
        }
    }

    @objc private func openCodex() {
        guard let codexURL else { return }
        NSWorkspace.shared.openApplication(at: codexURL, configuration: .init()) { _, error in
            if error != nil {
                DispatchQueue.main.async {
                    self.reopen.isHidden = false
                    self.statusDetail.stringValue += " 请手动打开 Codex。"
                }
            }
        }
    }

    @objc private func openGuide() { NSWorkspace.shared.open(guideURL) }

    @objc private func showBackup() {
        if let backup { NSWorkspace.shared.activateFileViewerSelecting([backup]) }
    }
}

if CommandLine.arguments.contains("--self-check") {
    let scriptPresent = Bundle.main.url(forResource: "sync_sidebar", withExtension: "py") != nil
    let enginePresent = Bundle.main.url(forResource: "recover", withExtension: "py") != nil
    let pythonAvailable = (try? python()) != nil
    print("resources=\(scriptPresent && enginePresent) python=\(pythonAvailable)")
    exit(scriptPresent && enginePresent && pythonAvailable ? 0 : 1)
}

let app = NSApplication.shared
private let delegate = App()
app.delegate = delegate
app.setActivationPolicy(.regular)
app.run()
