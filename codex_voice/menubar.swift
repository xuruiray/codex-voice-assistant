import AppKit
import Foundation
import WebKit
import Darwin

func makeIcon() -> NSImage {
    let image = NSImage(size: NSSize(width: 512, height: 512))
    image.lockFocus()
    NSColor(calibratedRed: 0.15, green: 0.28, blue: 0.26, alpha: 1).setFill()
    NSBezierPath(roundedRect: NSRect(x: 18, y: 18, width: 476, height: 476), xRadius: 112, yRadius: 112).fill()
    NSColor(calibratedRed: 0.81, green: 0.71, blue: 0.47, alpha: 1).setStroke()
    for (index, height) in [78.0, 180.0, 290.0, 180.0, 78.0].enumerated() {
        let path = NSBezierPath(); path.lineWidth = 22; path.lineCapStyle = .round
        let x = 136.0 + Double(index) * 60
        path.move(to: NSPoint(x: x, y: (512-height)/2)); path.line(to: NSPoint(x: x, y: (512+height)/2)); path.stroke()
    }
    image.unlockFocus(); return image
}

if let index = CommandLine.arguments.firstIndex(of: "--render-icon"), CommandLine.arguments.count > index + 1 {
    let image = makeIcon()
    if let tiff = image.tiffRepresentation, let bitmap = NSBitmapImageRep(data: tiff), let png = bitmap.representation(using: .png, properties: [:]) {
        try png.write(to: URL(fileURLWithPath: CommandLine.arguments[index + 1]))
    }
    exit(0)
}

@MainActor
final class VoiceApplication: NSObject, NSApplicationDelegate, WKNavigationDelegate {
    var statusItem: NSStatusItem!
    var window: NSWindow?
    var webView: WKWebView?
    var timer: Timer?
    var enabled = true
    var available = false
    var loadedOrigin: String?
    var loadedToken: String?
    var lockDescriptor: Int32 = -1
    var statusLine = NSMenuItem(title: "正在连接本机服务…", action: nil, keyEquivalent: "")
    var toggleItem: NSMenuItem!
    var testItem: NSMenuItem!
    var stateDirectory: URL {
        let args = CommandLine.arguments
        if let index = args.firstIndex(of: "--state-dir"), args.count > index + 1 { return URL(fileURLWithPath: args[index + 1]) }
        return FileManager.default.homeDirectoryForCurrentUser.appendingPathComponent("Library/Application Support/CodexVoice")
    }

    func endpoint() -> (URL, String)? {
        guard let data = try? Data(contentsOf: stateDirectory.appendingPathComponent("ui.json")),
              let object = try? JSONSerialization.jsonObject(with: data) as? [String: Any],
              let origin = object["origin"] as? String, let url = URL(string: origin),
              url.scheme == "http", url.host == "127.0.0.1", url.port != nil,
              let token = object["token"] as? String else { return nil }
        return (url, token)
    }

    func applicationDidFinishLaunching(_ notification: Notification) {
        try? FileManager.default.createDirectory(at: stateDirectory, withIntermediateDirectories: true)
        lockDescriptor = Darwin.open(stateDirectory.appendingPathComponent("menubar.lock").path, O_CREAT | O_RDWR, S_IRUSR | S_IWUSR)
        if lockDescriptor < 0 || Darwin.lockf(lockDescriptor, F_TLOCK, 0) != 0 {
            if CommandLine.arguments.contains("--show-settings") {
                DistributedNotificationCenter.default().postNotificationName(NSNotification.Name("io.github.codex-voice.open-settings"), object: nil, userInfo: nil, deliverImmediately: true)
            }
            NSApp.terminate(nil); return
        }
        DistributedNotificationCenter.default().addObserver(self, selector: #selector(showSettings), name: NSNotification.Name("io.github.codex-voice.open-settings"), object: nil)
        NSApp.setActivationPolicy(.accessory)
        NSApp.applicationIconImage = makeIcon()
        let main = NSMenu()
        let applicationRoot = NSMenuItem(); main.addItem(applicationRoot)
        let applicationMenu = NSMenu(); applicationRoot.submenu = applicationMenu
        add(applicationMenu, "打开设置…", #selector(showSettings), ",")
        applicationMenu.addItem(.separator()); add(applicationMenu, "退出菜单栏", #selector(quit), "q")
        let editRoot = NSMenuItem(title: "编辑", action: nil, keyEquivalent: ""); main.addItem(editRoot)
        let edit = NSMenu(title: "编辑"); editRoot.submenu = edit
        for (title, action, key) in [("撤销", "undo:", "z"), ("剪切", "cut:", "x"), ("复制", "copy:", "c"), ("粘贴", "paste:", "v"), ("全选", "selectAll:", "a")] {
            edit.addItem(NSMenuItem(title: title, action: Selector(action), keyEquivalent: key))
        }
        NSApp.mainMenu = main
        statusItem = NSStatusBar.system.statusItem(withLength: NSStatusItem.variableLength)
        statusItem.button?.image = NSImage(systemSymbolName: "waveform", accessibilityDescription: "Codex Voice")
        statusItem.button?.image?.isTemplate = true
        let menu = NSMenu()
        menu.addItem(NSMenuItem(title: "Codex Voice", action: nil, keyEquivalent: ""))
        menu.addItem(statusLine); menu.addItem(.separator())
        add(menu, "打开设置…", #selector(showSettings), ",")
        toggleItem = add(menu, "暂停提醒", #selector(toggle), "")
        testItem = add(menu, "试听当前声音", #selector(test), "")
        add(menu, "查看播报记录", #selector(showHistory), "")
        menu.addItem(.separator())
        add(menu, "退出菜单栏", #selector(quit), "q")
        statusItem.menu = menu
        poll()
        timer = Timer.scheduledTimer(withTimeInterval: 3, repeats: true) { [weak self] _ in Task { @MainActor in self?.poll() } }
        if CommandLine.arguments.contains("--show-settings") { showSettings() }
    }

    @discardableResult
    func add(_ menu: NSMenu, _ title: String, _ action: Selector, _ key: String) -> NSMenuItem {
        let item = NSMenuItem(title: title, action: action, keyEquivalent: key); item.target = self
        menu.addItem(item); return item
    }

    func request(_ route: String, body: [String: Any]? = nil, completion: @escaping ([String: Any]?) -> Void) {
        guard let (origin, token) = endpoint() else { completion(nil); return }
        var request = URLRequest(url: origin.appendingPathComponent("api/" + route)); request.timeoutInterval = 6
        request.setValue("Bearer " + token, forHTTPHeaderField: "Authorization")
        if let body = body { request.httpMethod = "POST"; request.setValue("application/json", forHTTPHeaderField: "Content-Type"); request.httpBody = try? JSONSerialization.data(withJSONObject: body) }
        URLSession.shared.dataTask(with: request) { data, response, error in
            let status = (response as? HTTPURLResponse)?.statusCode ?? 0
            let value = error == nil && (200...299).contains(status) ? data.flatMap { try? JSONSerialization.jsonObject(with: $0) as? [String: Any] } : nil
            DispatchQueue.main.async { completion(value) }
        }.resume()
    }

    func poll() {
        request("status") { [weak self] data in
            guard let self = self else { return }
            self.available = data?["running"] as? Bool ?? false
            self.enabled = data?["enabled"] as? Bool ?? self.enabled
            self.statusLine.title = !self.available ? "服务暂未连接" : self.enabled ? "正在监听 · 本机运行" : "语音已暂停 · 继续记录"
            self.toggleItem.title = self.enabled ? "暂停提醒" : "恢复提醒"
            self.toggleItem.isEnabled = self.available; self.testItem.isEnabled = self.available
            self.statusItem.button?.image = NSImage(systemSymbolName: self.enabled ? "waveform" : "bell.slash", accessibilityDescription: self.statusLine.title)
            self.statusItem.button?.image?.isTemplate = true
            let queued = (data?["stats"] as? [String: Any])?["queued"] as? Int ?? 0
            self.statusItem.button?.title = queued > 0 ? " \(queued)" : ""
            self.statusItem.button?.toolTip = "Codex Voice · " + self.statusLine.title
        }
    }

    @objc func toggle() { request("config", body: ["enabled": !enabled]) { [weak self] _ in self?.poll() } }
    @objc func test() { testItem.isEnabled = false; request("test", body: [:]) { [weak self] _ in self?.poll() } }
    @objc func showHistory() { show(page: "history") }
    @objc func showSettings() { show(page: "voice") }
    func show(page: String) {
        guard let (origin, token) = endpoint() else {
            let alert = NSAlert(); alert.messageText = "本机服务尚未启动"; alert.informativeText = "请运行 codex-voice install-service 后重新打开设置。"; alert.runModal(); return
        }
        if window == nil {
            let config = WKWebViewConfiguration(); config.websiteDataStore = .nonPersistent()
            let view = WKWebView(frame: .zero, configuration: config); view.navigationDelegate = self
            let created = NSWindow(contentRect: NSRect(x: 0, y: 0, width: 900, height: 520), styleMask: [.titled, .closable, .miniaturizable, .resizable], backing: .buffered, defer: false)
            created.title = "Codex Voice"; created.minSize = NSSize(width: 760, height: 440)
            created.isReleasedWhenClosed = false; created.titlebarAppearsTransparent = true
            created.backgroundColor = NSColor(calibratedRed: 0.95, green: 0.94, blue: 0.90, alpha: 1); created.contentView = view
            created.center(); window = created; webView = view
        }
        var url = URLComponents(url: origin, resolvingAgainstBaseURL: false)!
        // A fragment-only change is same-document navigation in WKWebView;
        // rotate a non-secret query value so a new server token reloads the JS.
        url.queryItems = [URLQueryItem(name: "page", value: page), URLQueryItem(name: "connection", value: UUID().uuidString)]; url.fragment = "token=" + token
        if loadedOrigin == origin.absoluteString && loadedToken == token && webView?.url != nil {
            webView?.evaluateJavaScript("go('\(page)')", completionHandler: nil)
        } else {
            webView?.load(URLRequest(url: url.url!)); loadedOrigin = origin.absoluteString; loadedToken = token
        }
        if window?.isMiniaturized == true { window?.deminiaturize(nil) }
        window?.makeKeyAndOrderFront(nil); NSApp.activate(ignoringOtherApps: true)
    }

    func webView(_ webView: WKWebView, decidePolicyFor navigationAction: WKNavigationAction, decisionHandler: @escaping (WKNavigationActionPolicy) -> Void) {
        guard let url = navigationAction.request.url, let (origin, _) = endpoint(), url.scheme == origin.scheme, url.host == origin.host, url.port == origin.port else { decisionHandler(.cancel); return }
        decisionHandler(.allow)
    }
    func applicationShouldHandleReopen(_ sender: NSApplication, hasVisibleWindows flag: Bool) -> Bool { showSettings(); return true }
    func applicationShouldTerminateAfterLastWindowClosed(_ sender: NSApplication) -> Bool { return false }
    @objc func quit() { NSApp.terminate(nil) }
}

MainActor.assumeIsolated {
    let app = NSApplication.shared
    let delegate = VoiceApplication()
    app.delegate = delegate
    app.run()
}
