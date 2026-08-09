import AppKit
import Darwin
import Foundation
import ServiceManagement
import UniformTypeIdentifiers
import UserNotifications

// MARK: - Status

private enum SkinStatus: Equatable {
    case hermesStopped
    case hermesNeedsAttach
    case attaching
    case active
    case attachedWithoutInjector
    case error(String)

    var label: String {
        switch self {
        case .hermesStopped: return "Hermes 未运行"
        case .hermesNeedsAttach: return "Hermes 已运行 · 需要重新连接"
        case .attaching: return "正在启动并连接…"
        case .active: return "皮肤已生效"
        case .attachedWithoutInjector: return "Hermes 已连接 · 皮肤未运行"
        case .error(let message): return "异常 · \(message)"
        }
    }
}

// MARK: - Launcher

@MainActor
private final class LauncherController: NSObject, NSApplicationDelegate {
    private let statusItem = NSStatusBar.system.statusItem(withLength: NSStatusItem.variableLength)
    private var timer: Timer?
    private var status: SkinStatus = .hermesStopped
    private var injectorProcess: Process?
    private let fileManager = FileManager.default
    private let port = 9334
    private var selectedTheme = "ada-sofa"

    // SF Symbol menu bar icon (vector, auto-adapts to light/dark)
    private lazy var menuBarIcon: NSImage? = {
        let config = NSImage.SymbolConfiguration(pointSize: 14, weight: .medium)
        let image = NSImage(systemSymbolName: "paintpalette", accessibilityDescription: nil)?
            .withSymbolConfiguration(config)
        image?.isTemplate = true
        return image
    }()

    // State directories
    private lazy var supportRoot = fileManager.homeDirectoryForCurrentUser
        .appendingPathComponent("Library/Application Support/HermesSkinLauncher", isDirectory: true)
    private lazy var stateRoot = supportRoot.appendingPathComponent("state", isDirectory: true)
    private lazy var logURL = stateRoot.appendingPathComponent("launcher.log")

    // MARK: - Lifecycle

    func applicationDidFinishLaunching(_ notification: Notification) {
        NSApplication.shared.setActivationPolicy(.accessory)
        statusItem.button?.image = menuBarIcon
        statusItem.button?.toolTip = "Hermes Skin"
        loadSelectedTheme()
        rebuildMenu()
        refreshStatus()
        timer = Timer.scheduledTimer(withTimeInterval: 2, repeats: true) { [weak self] _ in
            Task { @MainActor in self?.refreshStatus() }
        }
    }

    func applicationWillTerminate(_ notification: Notification) {
        timer?.invalidate()
    }

    // MARK: - Engine root resolution

    private var engineRoot: URL? {
        // 1. Environment variable
        if let configured = ProcessInfo.processInfo.environment["HERMES_SKIN_ROOT"], !configured.isEmpty {
            let url = URL(fileURLWithPath: configured, isDirectory: true)
            if fileManager.fileExists(atPath: url.appendingPathComponent("runtime/injector-hermes.py").path) { return url }
        }
        // 2. Support root (installed runtime)
        if fileManager.fileExists(atPath: supportRoot.appendingPathComponent("runtime/injector-hermes.py").path) { return supportRoot }
        // 3. Bundled Engine directory (inside .app)
        if let bundled = Bundle.main.resourceURL?.appendingPathComponent("Engine", isDirectory: true),
           fileManager.fileExists(atPath: bundled.appendingPathComponent("runtime/injector-hermes.py").path) { return bundled }
        // 4. Current working directory (dev mode)
        let working = URL(fileURLWithPath: fileManager.currentDirectoryPath, isDirectory: true)
        if fileManager.fileExists(atPath: working.appendingPathComponent("runtime/injector-hermes.py").path) { return working }
        return nil
    }

    // MARK: - Hermes app discovery

    private var hermesURL: URL? {
        // Check via bundle identifier first
        if let discovered = NSWorkspace.shared.urlForApplication(withBundleIdentifier: "com.nousresearch.hermes") {
            return discovered
        }
        let path = "/Applications/Hermes.app"
        return fileManager.fileExists(atPath: path) ? URL(fileURLWithPath: path, isDirectory: true) : nil
    }

    private var hermesProcesses: [NSRunningApplication] {
        NSRunningApplication.runningApplications(withBundleIdentifier: "com.nousresearch.hermes")
    }

    // MARK: - Theme management

    /// User themes live in ~/Documents/Hermes Skin/themes/ — persistent
    /// across app updates. Built-in themes (ada-sofa) stay read-only in
    /// the .app bundle as a permanent fallback. availableThemes() merges
    /// both sources; user dir takes priority on id collision.
    private lazy var userThemesRoot: URL = {
        fileManager.homeDirectoryForCurrentUser
            .appendingPathComponent("Documents/Hermes Skin/themes", isDirectory: true)
    }()

    /// Read-only built-in themes shipped inside the .app bundle.
    private func bundledThemesRoot(for root: URL) -> URL {
        root.appendingPathComponent("runtime/themes-hermes", isDirectory: true)
    }

    /// Ensure the user themes directory exists (created on demand).
    private func ensureUserThemesDir() {
        if !fileManager.fileExists(atPath: userThemesRoot.path) {
            try? fileManager.createDirectory(at: userThemesRoot, withIntermediateDirectories: true)
        }
    }

    /// Merge built-in and user themes. User themes override built-in on
    /// id collision (so users can customize a copy). Returns (id, name)
    /// pairs sorted by name.
    private func availableThemes() -> [(id: String, name: String)] {
        guard let root = engineRoot else { return [] }
        ensureUserThemesDir()
        var byId: [String: (name: String, isUser: Bool)] = [:]
        // 1. Built-in themes (lower priority)
        let bundledDir = bundledThemesRoot(for: root)
        if let entries = try? fileManager.contentsOfDirectory(at: bundledDir, includingPropertiesForKeys: [.isDirectoryKey]) {
            for dir in entries.sorted(by: { $0.lastPathComponent < $1.lastPathComponent }) {
                guard let pair = parseTheme(at: dir) else { continue }
                byId[pair.id] = (pair.name, false)
            }
        }
        // 2. User themes (higher priority — override built-in)
        if let entries = try? fileManager.contentsOfDirectory(at: userThemesRoot, includingPropertiesForKeys: [.isDirectoryKey]) {
            for dir in entries.sorted(by: { $0.lastPathComponent < $1.lastPathComponent }) {
                guard let pair = parseTheme(at: dir) else { continue }
                byId[pair.id] = (pair.name, true)
            }
        }
        return byId.values.map { ($0.name, $0.name) }.isEmpty
            ? []
            : byId.sorted { $0.value.name < $1.value.name }.map { ($0.key, $0.value.name) }
    }

    /// Parse a theme directory and return (id, name) if valid.
    private func parseTheme(at dir: URL) -> (id: String, name: String)? {
        let themeJson = dir.appendingPathComponent("theme.json")
        guard fileManager.fileExists(atPath: themeJson.path),
              let data = try? Data(contentsOf: themeJson),
              let json = try? JSONSerialization.jsonObject(with: data) as? [String: Any],
              let id = json["id"] as? String else { return nil }
        let name = (json["name"] as? String) ?? id
        return (id, name)
    }

    private func loadSelectedTheme() {
        let file = stateRoot.appendingPathComponent("selected-theme")
        if let saved = try? String(contentsOf: file, encoding: .utf8) {
            let trimmed = saved.trimmingCharacters(in: .whitespacesAndNewlines)
            if !trimmed.isEmpty { selectedTheme = trimmed }
        }
    }

    private func saveSelectedTheme() {
        let file = stateRoot.appendingPathComponent("selected-theme")
        try? selectedTheme.write(to: file, atomically: true, encoding: .utf8)
    }

    /// Resolve the theme directory: check user dir first, fall back to
    /// the built-in bundled copy. This way ada-sofa always resolves even
    /// if the user deleted it from ~/Documents.
    private func themeDirPath(for root: URL) -> String {
        let userDir = userThemesRoot.appendingPathComponent(selectedTheme)
        if fileManager.fileExists(atPath: userDir.appendingPathComponent("theme.json").path) {
            return userDir.path
        }
        // Fall back to built-in
        return bundledThemesRoot(for: root).appendingPathComponent(selectedTheme).path
    }

    /// The root to use for theme CRUD (create/delete/rename): always the
    /// user directory — built-in themes are read-only.
    private func themesRoot(for root: URL) -> URL {
        ensureUserThemesDir()
        return userThemesRoot
    }

    // MARK: - Theme color extraction (image-based theme generation)

    /// Downsamples the image to 64x64 and extracts a coherent color palette.
    /// - background: darkest 10% average, darkened to target luminance <= 0.08
    /// - accent: brightest saturated pixel (scored by sat*lum)
    private func extractThemeColors(from imageURL: URL) -> [String: String]? {
        guard let cgImageSource = CGImageSourceCreateWithURL(imageURL as CFURL, nil),
              let cgImage = CGImageSourceCreateImageAtIndex(cgImageSource, 0, nil) else { return nil }

        let n = 64
        let bpp = 4
        var pixels = [UInt8](repeating: 0, count: n * n * bpp)
        let cs = CGColorSpaceCreateDeviceRGB()
        guard let ctx = CGContext(data: &pixels, width: n, height: n, bitsPerComponent: 8,
                                  bytesPerRow: n * bpp, space: cs,
                                  bitmapInfo: CGImageAlphaInfo.premultipliedLast.rawValue) else { return nil }
        ctx.draw(cgImage, in: CGRect(x: 0, y: 0, width: n, height: n))

        struct Px { let r, g, b, lum, sat: Double }

        var all: [Px] = []
        all.reserveCapacity(n * n)
        for i in 0..<(n * n) {
            let r = Double(pixels[i * 4]) / 255
            let g = Double(pixels[i * 4 + 1]) / 255
            let b = Double(pixels[i * 4 + 2]) / 255
            let mx = max(r, max(g, b)), mn = min(r, min(g, b))
            all.append(Px(r: r, g: g, b: b,
                          lum: (mx + mn) / 2,
                          sat: mx == 0 ? 0 : (mx - mn) / mx))
        }

        let dark = all.sorted { $0.lum < $1.lum }
        let dc = max(1, Int(Double(all.count) * 0.10))
        let dp = Array(dark.prefix(dc))
        var bgR = dp.map { $0.r }.reduce(0, +) / Double(dc)
        var bgG = dp.map { $0.g }.reduce(0, +) / Double(dc)
        var bgB = dp.map { $0.b }.reduce(0, +) / Double(dc)
        let bgLum = (max(bgR, max(bgG, bgB)) + min(bgR, min(bgG, bgB))) / 2
        if bgLum > 0.08 {
            let factor = 0.08 / bgLum
            bgR *= factor; bgG *= factor; bgB *= factor
        }

        let cands = all.filter { $0.lum > 0.35 && $0.sat > 0.12 }
        let ac: Px
        if let best = cands.max(by: { ($0.sat * $0.lum) < ($1.sat * $1.lum) }) { ac = best }
        else { ac = Px(r: 0.83, g: 0.69, b: 0.22, lum: 0.75, sat: 0.37) }

        func hex(_ r: Double, _ g: Double, _ b: Double) -> String {
            String(format: "#%02x%02x%02x", Int(r * 255), Int(g * 255), Int(b * 255))
        }
        func lighter(_ r: Double, _ g: Double, _ b: Double, _ d: Double) -> String {
            hex(min(1, r + d), min(1, g + d), min(1, b + d))
        }

        return [
            "background": hex(bgR, bgG, bgB),
            "panel":      lighter(bgR, bgG, bgB, 0.03),
            "panelAlt":   lighter(bgR, bgG, bgB, 0.06),
            "accent":     hex(ac.r, ac.g, ac.b),
            "accentAlt":  hex(ac.r * 0.82, ac.g * 0.82, ac.b * 0.82),
            "secondary":  lighter(bgR, bgG, bgB, 0.12),
            "highlight":  hex(ac.r, ac.g, ac.b),
            "text":       "#e8e6e0",
            "muted":      lighter(bgR, bgG, bgB, 0.30),
            "line":       "rgba(\(Int(ac.r * 255)), \(Int(ac.g * 255)), \(Int(ac.b * 255)), .18)",
        ]
    }

    // MARK: - Theme ID generation

    private func slugify(_ name: String) -> String {
        let lowered = name.lowercased()
        var result = ""
        for ch in lowered {
            if ch.isLetter || ch.isNumber { result.append(ch) }
            else if ch == " " || ch == "-" || ch == "_" { result.append("-") }
            else { result.append("-") }
        }
        while result.contains("--") { result = result.replacingOccurrences(of: "--", with: "-") }
        result = result.trimmingCharacters(in: CharacterSet(charactersIn: "-"))
        if result.isEmpty { result = "theme-\(Int(Date().timeIntervalSince1970))" }
        return result
    }

    // MARK: - Dialogs

    private func textInputDialog(title: String, message: String, defaultValue: String) -> String? {
        let alert = NSAlert()
        alert.messageText = title
        alert.informativeText = message
        alert.addButton(withTitle: "确定")
        alert.addButton(withTitle: "取消")
        let tf = NSTextField(frame: NSRect(x: 0, y: 0, width: 260, height: 24))
        tf.stringValue = defaultValue
        alert.accessoryView = tf
        alert.window.initialFirstResponder = tf
        guard alert.runModal() == .alertFirstButtonReturn else { return nil }
        return tf.stringValue.isEmpty ? nil : tf.stringValue
    }

    private func themeSelectionDialog(title: String, message: String) -> (id: String, name: String)? {
        let themes = availableThemes()
        guard !themes.isEmpty else { return nil }
        let alert = NSAlert()
        alert.messageText = title
        alert.informativeText = message
        alert.addButton(withTitle: "确定")
        alert.addButton(withTitle: "取消")
        let popup = NSPopUpButton(frame: NSRect(x: 0, y: 0, width: 260, height: 24))
        for theme in themes { popup.addItem(withTitle: theme.name) }
        alert.accessoryView = popup
        guard alert.runModal() == .alertFirstButtonReturn else { return nil }
        let sel = popup.indexOfSelectedItem
        return sel >= 0 && sel < themes.count ? themes[sel] : nil
    }

    // MARK: - Theme CRUD actions

    @objc private func createTheme() {
        guard let root = engineRoot else { showError("找不到主题目录。") ; return }
        let panel = NSOpenPanel()
        panel.title = "选择主题背景图"
        panel.allowedContentTypes = [.png, .jpeg, .image]
        guard panel.runModal() == .OK, let imageURL = panel.url else { return }
        guard let themeName = textInputDialog(title: "新建主题", message: "输入主题名称（如：My Theme）", defaultValue: "") else { return }

        var themeId = slugify(themeName)
        let themesDir = themesRoot(for: root)
        try? fileManager.createDirectory(at: themesDir, withIntermediateDirectories: true)
        // Collision check
        if fileManager.fileExists(atPath: themesDir.appendingPathComponent(themeId).path) {
            var counter = 2
            while fileManager.fileExists(atPath: themesDir.appendingPathComponent("\(themeId)-\(counter)").path) { counter += 1 }
            themeId = "\(themeId)-\(counter)"
        }
        let themeDir = themesDir.appendingPathComponent(themeId)
        do {
            try fileManager.createDirectory(at: themeDir, withIntermediateDirectories: true)
            let ext = imageURL.pathExtension
            let imageName = "\(themeId).\(ext)"
            try fileManager.copyItem(at: imageURL, to: themeDir.appendingPathComponent(imageName))

            guard let colors = extractThemeColors(from: imageURL) else {
                showError("无法从图片提取颜色。") ; return
            }
            let themeJson: [String: Any] = [
                "schemaVersion": 1,
                "id": themeId,
                "name": themeName,
                "brandSubtitle": themeName.uppercased(),
                "tagline": "",
                "quote": "",
                "image": imageName,
                "appearance": "dark",
                "colors": colors,
                "art": [
                    "safeArea": "72%",
                    "focusX": "50%",
                    "focusY": "35%",
                    "taskMode": "dim"
                ]
            ]
            let data = try JSONSerialization.data(withJSONObject: themeJson, options: [.prettyPrinted, .sortedKeys])
            try data.write(to: themeDir.appendingPathComponent("theme.json"))

            selectedTheme = themeId
            saveSelectedTheme()
        } catch {
            showError("无法创建主题：\(error.localizedDescription)") ; return
        }
        rebuildMenu()
        if cdpAvailable() {
            restartInjectorAsync()
        }
    }

    @objc private func deleteTheme() {
        guard let root = engineRoot else { return }
        let themes = availableThemes()
        guard themes.count > 1 else { showError("至少需要保留一个主题。") ; return }
        guard let selection = themeSelectionDialog(title: "删除主题", message: "选择要删除的主题") else { return }
        // Built-in themes cannot be deleted
        let bundledDir = bundledThemesRoot(for: root).appendingPathComponent(selection.id)
        if fileManager.fileExists(atPath: bundledDir.appendingPathComponent("theme.json").path) {
            showError("内置主题「\(selection.name)」无法删除。")
            return
        }
        let themesRootURL = themesRoot(for: root)
        let themeDir = themesRootURL.appendingPathComponent(selection.id)
        let alert = NSAlert()
        alert.messageText = "确认删除"
        alert.informativeText = "确定要删除主题「\(selection.name)」吗？此操作不可撤销。"
        alert.alertStyle = .warning
        alert.addButton(withTitle: "删除")
        alert.addButton(withTitle: "取消")
        guard alert.runModal() == .alertFirstButtonReturn else { return }
        try? fileManager.removeItem(at: themeDir)
        if selectedTheme == selection.id {
            selectedTheme = themes.first(where: { $0.id != selection.id })?.id ?? "ada-sofa"
            saveSelectedTheme()
            if cdpAvailable() {
                restartInjectorAsync()
            }
        }
        rebuildMenu()
    }

    @objc private func renameTheme() {
        guard let root = engineRoot else { return }
        guard let selection = themeSelectionDialog(title: "重命名主题", message: "选择要重命名的主题") else { return }
        guard let newName = textInputDialog(title: "重命名主题", message: "输入新的主题名称", defaultValue: selection.name) else { return }
        let themeDir = themesRoot(for: root).appendingPathComponent(selection.id)
        let jsonPath = themeDir.appendingPathComponent("theme.json")
        guard let data = try? Data(contentsOf: jsonPath),
              var json = try? JSONSerialization.jsonObject(with: data) as? [String: Any] else { return }
        json["name"] = newName
        json["brandSubtitle"] = newName.uppercased()
        guard let newData = try? JSONSerialization.data(withJSONObject: json, options: [.prettyPrinted, .sortedKeys]) else { return }
        try? newData.write(to: jsonPath)
        rebuildMenu()
    }

    @objc private func selectTheme(_ sender: NSMenuItem) {
        guard let id = sender.representedObject as? String else { return }
        let themeName = sender.title
        selectedTheme = id
        saveSelectedTheme()
        rebuildMenu()
        // Always restart the injector with the new theme when Hermes is
        // reachable - even if the injector process died, so a theme switch
        // is never a no-op (previously gated on injectorRunning()).
        if cdpAvailable() {
            restartInjectorAsync()
            notifyThemeSwitch(name: themeName)
        }
    }

    // MARK: - Theme switch notification

    /// Show a non-intrusive macOS notification when a theme switch succeeds.
    /// Requests notification authorization on first use, then schedules a
    /// short-lived UNNotification with the theme name.
    private func notifyThemeSwitch(name: String) {
        UNUserNotificationCenter.current().requestAuthorization(options: [.alert]) { granted, _ in
            guard granted else { return }
            let content = UNMutableNotificationContent()
            content.title = "Hermes Skin"
            content.body = "已切换至「\(name)」"
            content.sound = nil
            let req = UNNotificationRequest(
                identifier: "theme-switch-\(Date().timeIntervalSince1970)",
                content: content, trigger: nil)
            UNUserNotificationCenter.current().add(req)
        }
    }

    // MARK: - Menu

    private func rebuildMenu() {
        let menu = NSMenu()
        let statusRow = NSMenuItem(title: status.label, action: nil, keyEquivalent: "")
        statusRow.isEnabled = false
        menu.addItem(statusRow)
        menu.addItem(.separator())
        menu.addItem(item("启动并连接 Hermes", #selector(launchAndAttach), "play.fill"))
        menu.addItem(item("重新应用皮肤", #selector(reapply), "arrow.clockwise"))
        menu.addItem(item("恢复原生界面", #selector(restoreOriginal), "arrow.uturn.backward"))
        menu.addItem(.separator())

        // Theme selector submenu
        let themeMenu = NSMenu()
        let themeMenuItem = NSMenuItem(title: "切换皮肤", action: nil, keyEquivalent: "")
        themeMenuItem.image = NSImage(systemSymbolName: "paintpalette", accessibilityDescription: nil)
        let themes = availableThemes()
        if themes.isEmpty {
            let empty = NSMenuItem(title: "（未找到主题）", action: nil, keyEquivalent: "")
            empty.isEnabled = false
            themeMenu.addItem(empty)
        } else {
            for theme in themes {
                let row = NSMenuItem(title: theme.name, action: #selector(selectTheme(_:)), keyEquivalent: "")
                row.target = self
                row.representedObject = theme.id
                if theme.id == selectedTheme { row.state = .on }
                themeMenu.addItem(row)
            }
        }
        themeMenuItem.submenu = themeMenu
        menu.addItem(themeMenuItem)

        // Theme management submenu
        let manageMenu = NSMenu()
        let manageItem = NSMenuItem(title: "皮肤管理", action: nil, keyEquivalent: "")
        manageItem.image = NSImage(systemSymbolName: "slider.horizontal.3", accessibilityDescription: nil)
        manageMenu.addItem(item("新建主题…", #selector(createTheme), "plus.circle"))
        manageMenu.addItem(item("重命名主题…", #selector(renameTheme), "pencil"))
        manageMenu.addItem(item("删除主题…", #selector(deleteTheme), "trash"))
        manageItem.submenu = manageMenu
        menu.addItem(manageItem)

        menu.addItem(item("运行诊断", #selector(runDiagnostics), "stethoscope"))
        menu.addItem(item("打开主题目录", #selector(openThemeFolder), "folder"))
        menu.addItem(item("打开日志", #selector(openLogs), "doc.text.magnifyingglass"))

        let login = item("登录时启动", #selector(toggleLoginItem), "power")
        login.state = SMAppService.mainApp.status == .enabled ? .on : .off
        menu.addItem(login)

        menu.addItem(.separator())
        menu.addItem(item("退出启动器", #selector(quitLauncher), "xmark.circle"))
        statusItem.menu = menu
    }

    private func item(_ title: String, _ action: Selector, _ symbol: String) -> NSMenuItem {
        let item = NSMenuItem(title: title, action: action, keyEquivalent: "")
        item.target = self
        item.image = NSImage(systemSymbolName: symbol, accessibilityDescription: nil)
        return item
    }

    // MARK: - Status

    private func refreshStatus() {
        guard status != .attaching else { return }
        let running = !hermesProcesses.isEmpty
        let cdp = cdpAvailable()
        let injector = injectorRunning()
        status = !running ? .hermesStopped : !cdp ? .hermesNeedsAttach : injector ? .active : .attachedWithoutInjector
        statusItem.button?.image = menuBarIcon
        statusItem.button?.toolTip = "Hermes Skin · \(status.label)"
        rebuildMenu()
    }

    private func cdpAvailable() -> Bool {
        let process = Process()
        process.executableURL = URL(fileURLWithPath: "/usr/bin/curl")
        process.arguments = ["-fsS", "--max-time", "1", "http://127.0.0.1:\(port)/json/version"]
        process.standardOutput = FileHandle.nullDevice
        process.standardError = FileHandle.nullDevice
        do { try process.run(); process.waitUntilExit(); return process.terminationStatus == 0 } catch { return false }
    }

    private func injectorRunning() -> Bool {
        guard let contents = try? String(contentsOf: stateRoot.appendingPathComponent("injector.pid"), encoding: .utf8),
              let pid = Int32(contents.trimmingCharacters(in: .whitespacesAndNewlines)), pid > 1 else { return false }
        return kill(pid, 0) == 0
    }

    // MARK: - Launch & attach

    @objc private func launchAndAttach() {
        guard let appURL = hermesURL else { showError("没有找到 Hermes.app。请先安装并至少启动一次。") ; return }
        guard engineRoot != nil else { showError("没有找到皮肤运行引擎。请重新安装 Hermes Skin。") ; return }
        if !hermesProcesses.isEmpty && !cdpAvailable() {
            let alert = NSAlert()
            alert.messageText = "需要重新启动 Hermes"
            alert.informativeText = "当前 Hermes 没有开放本机皮肤连接。启动器需要先正常退出 Hermes，再使用调试端口重新启动。"
            alert.addButton(withTitle: "重新启动并连接")
            alert.addButton(withTitle: "取消")
            guard alert.runModal() == .alertFirstButtonReturn else { return }
            hermesProcesses.forEach { $0.terminate() }
        }
        status = .attaching
        rebuildMenu()
        Task { await launchSequence(appURL: appURL) }
    }

    private func launchSequence(appURL: URL) async {
        // Wait for old processes to exit
        let deadline = Date().addingTimeInterval(6)
        while !hermesProcesses.isEmpty && Date() < deadline {
            try? await Task.sleep(for: .milliseconds(150))
        }

        // Launch Hermes with CDP if port not yet available
        if !cdpAvailable() {
            let configuration = NSWorkspace.OpenConfiguration()
            configuration.arguments = [
                "--remote-debugging-port=\(port)",
                "--user-data-dir=/tmp/hermes-skin-cdp"
            ]
            configuration.createsNewApplicationInstance = true
            await withCheckedContinuation { continuation in
                NSWorkspace.shared.openApplication(at: appURL, configuration: configuration) { _, _ in continuation.resume() }
            }
        }

        // Wait for CDP to become available
        let readyDeadline = Date().addingTimeInterval(15)
        while !cdpAvailable() && Date() < readyDeadline {
            try? await Task.sleep(for: .milliseconds(250))
        }
        guard cdpAvailable() else {
            status = .error("CDP 未就绪")
            rebuildMenu()
            return
        }

        startInjector()
        try? await Task.sleep(for: .milliseconds(800))
        refreshStatus()
    }

    // MARK: - Injector process

    /// Stop the running injector and wait until the old process has fully
    /// exited (polling kill(pid, 0)) before returning. This eliminates the
    /// two-click race: previously startInjector's `guard !injectorRunning()`
    /// blocked the restart while the old process was still shutting down.
    private func stopInjectorAndWait() {
        _ = runScript("stop-skin-macos.sh")
        let deadline = Date().addingTimeInterval(4)
        while injectorRunning() && Date() < deadline {
            Thread.sleep(forTimeInterval: 0.15)
        }
        // Ensure no stale PID file survives to confuse the next start.
        try? fileManager.removeItem(at: stateRoot.appendingPathComponent("injector.pid"))
    }

    /// Restart the injector with the currently selected theme: stop the old
    /// process, wait for it to fully exit (kills the two-click race), then
    /// start a fresh injector. Runs synchronously on the main actor like the
    /// original code — the stop script itself already blocks briefly.
    private func restartInjectorAsync() {
        stopInjectorAndWait()
        startInjector()
    }

    private func startInjector() {
        guard !injectorRunning(), let root = engineRoot else { return }
        try? fileManager.createDirectory(at: stateRoot, withIntermediateDirectories: true)
        if !fileManager.fileExists(atPath: logURL.path) { _ = fileManager.createFile(atPath: logURL.path, contents: nil) }
        guard let log = try? FileHandle(forWritingTo: logURL) else { return }
        _ = try? log.seekToEnd()

        // Use Hermes venv Python (has websockets) or fall back to system python3
        let homePython = NSHomeDirectory() + "/.hermes/hermes-agent/venv/bin/python3"
        let pythonBin = fileManager.fileExists(atPath: homePython) ? homePython : "/usr/bin/env python3"
        let injector = root.appendingPathComponent("runtime/injector-hermes.py").path
        let themeDir = themeDirPath(for: root)

        let process = Process()
        process.executableURL = URL(fileURLWithPath: pythonBin)
        process.arguments = [injector, "--watch", "--port", String(port), "--theme-dir", themeDir]
        process.currentDirectoryURL = root
        process.standardOutput = log
        process.standardError = log
        // Capture path up front: terminationHandler is a Sendable closure
        // and cannot reference the @MainActor-isolated stateRoot directly.
        let pidPath = stateRoot.appendingPathComponent("injector.pid")
        process.terminationHandler = { [weak self] proc in
            try? log.close()
            // Write to stateRoot (same path injectorRunning() reads), not
            // root/state — previously a stale PID here made theme switches
            // require two clicks (guard in startInjector blocked the restart
            // while the old process was still shutting down).
            try? String(proc.processIdentifier).write(to: pidPath, atomically: true, encoding: .utf8)
            DispatchQueue.main.async { self?.refreshStatus() }
        }
        do {
            try process.run()
            injectorProcess = process
            let pid = String(process.processIdentifier)
            try? pid.write(to: stateRoot.appendingPathComponent("injector.pid"), atomically: true, encoding: .utf8)
        } catch { try? log.close() }
    }

    // MARK: - Shell scripts

    private func runScript(_ name: String, arguments: [String] = [], wait: Bool = true) -> Int32 {
        guard let root = engineRoot else { return 127 }
        try? fileManager.createDirectory(at: stateRoot, withIntermediateDirectories: true)
        if !fileManager.fileExists(atPath: logURL.path) { _ = fileManager.createFile(atPath: logURL.path, contents: nil) }
        let log = try? FileHandle(forWritingTo: logURL)
        _ = try? log?.seekToEnd()
        let process = Process()
        process.executableURL = URL(fileURLWithPath: "/bin/bash")
        process.arguments = [root.appendingPathComponent("scripts/\(name)").path] + arguments
        process.currentDirectoryURL = root
        process.standardOutput = log ?? FileHandle.nullDevice
        process.standardError = log ?? FileHandle.nullDevice
        do {
            try process.run()
            if wait { process.waitUntilExit(); try? log?.close() }
            else { process.terminationHandler = { _ in try? log?.close() } }
            return wait ? process.terminationStatus : 0
        } catch {
            try? log?.close()
            return 126
        }
    }

    // MARK: - Actions

    @objc private func reapply() {
        restartInjectorAsync()
        Task {
            try? await Task.sleep(for: .milliseconds(800))
            refreshStatus()
        }
    }

    @objc private func pauseSkin() { stopSkin(showConfirmation: false) }
    @objc private func restoreOriginal() { stopSkin(showConfirmation: true) }

    private func stopSkin(showConfirmation: Bool) {
        Task {
            let code = runScript("stop-skin-macos.sh")
            refreshStatus()
            if showConfirmation {
                let alert = NSAlert()
                alert.messageText = code == 0 ? "已恢复原生界面" : "恢复结果未确认"
                alert.informativeText = code == 0 ? "注入器已停止并清除了当前 Hermes 的皮肤。" : "没有收到完整清理确认；如仍有残留，请正常退出并重新打开 Hermes。"
                alert.runModal()
            }
        }
    }

    @objc private func runDiagnostics() {
        Task {
            let code = runScript("doctor-macos.sh", arguments: cdpAvailable() ? ["--live"] : [])
            let alert = NSAlert()
            alert.messageText = code == 0 ? "诊断通过" : "诊断发现问题"
            alert.informativeText = "退出码：\(code)。详细输出已写入启动器日志；当前连接状态：\(status.label)。"
            alert.runModal()
        }
    }

    @objc private func openThemeFolder() {
        guard let root = engineRoot else { showError("找不到主题目录。") ; return }
        NSWorkspace.shared.open(themesRoot(for: root))
    }

    @objc private func openLogs() {
        try? fileManager.createDirectory(at: stateRoot, withIntermediateDirectories: true)
        NSWorkspace.shared.open(stateRoot)
    }

    @objc private func toggleLoginItem() {
        do {
            if SMAppService.mainApp.status == .enabled { try SMAppService.mainApp.unregister() }
            else { try SMAppService.mainApp.register() }
        } catch { showError("无法更新登录项：\(error.localizedDescription)") }
        rebuildMenu()
    }

    @objc private func quitLauncher() { NSApplication.shared.terminate(nil) }

    // MARK: - Error

    private func showError(_ message: String) {
        let alert = NSAlert()
        alert.messageText = "Hermes Skin"
        alert.informativeText = message
        alert.alertStyle = .warning
        alert.runModal()
    }
}

// MARK: - Entry point

@main
private enum HermesSkinLauncherMain {
    static func main() {
        let app = NSApplication.shared
        let delegate = LauncherController()
        app.delegate = delegate
        app.run()
    }
}
