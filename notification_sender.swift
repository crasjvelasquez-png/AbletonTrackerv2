// A native app identity gives macOS Tracker's name and icon, rather than Python's.
// Usage: tracker-notify title subtitle message [category day] | --status
// When a notification action is chosen, macOS relaunches this helper with no
// arguments; the response is appended to notification_actions.jsonl for the
// menu-bar app to apply.
import AppKit
import UserNotifications

let args = CommandLine.arguments.filter { !$0.hasPrefix("-psn") }
let center = UNUserNotificationCenter.current()
let actionsURL = FileManager.default.homeDirectoryForCurrentUser
    .appendingPathComponent(".ableton_tracker/notification_actions.jsonl")
var finished = false
var result: Int32 = 1
func finish(_ code: Int32, _ message: String = "") {
    DispatchQueue.main.async {
        if !message.isEmpty { fputs(message + "\n", stderr) }
        result = code
        finished = true
    }
}

func openAbleton() {
    let workspace = NSWorkspace.shared
    if let url = workspace.urlForApplication(withBundleIdentifier: "com.ableton.live") {
        workspace.openApplication(at: url, configuration: NSWorkspace.OpenConfiguration())
        return
    }
    let apps = (try? FileManager.default.contentsOfDirectory(atPath: "/Applications")) ?? []
    if let name = apps.filter({ $0.hasPrefix("Ableton Live") && $0.hasSuffix(".app") }).sorted().last {
        workspace.openApplication(at: URL(fileURLWithPath: "/Applications/" + name),
                                  configuration: NSWorkspace.OpenConfiguration())
    }
}

func record(_ action: String, day: String) {
    let line = (try? JSONSerialization.data(withJSONObject: [
        "action": action, "day": day, "at": Date().timeIntervalSince1970,
    ])).flatMap { String(data: $0, encoding: .utf8) } ?? ""
    guard !line.isEmpty, let data = (line + "\n").data(using: .utf8) else { return }
    try? FileManager.default.createDirectory(at: actionsURL.deletingLastPathComponent(),
                                             withIntermediateDirectories: true)
    if let handle = try? FileHandle(forWritingTo: actionsURL) {
        handle.seekToEndOfFile()
        handle.write(data)
        handle.closeFile()
    } else {
        try? data.write(to: actionsURL)
    }
}

class Delegate: NSObject, UNUserNotificationCenterDelegate {
    func userNotificationCenter(_ center: UNUserNotificationCenter,
                                didReceive response: UNNotificationResponse,
                                withCompletionHandler completionHandler: @escaping () -> Void) {
        let day = response.notification.request.content.userInfo["day"] as? String ?? ""
        switch response.actionIdentifier {
        case "later": record("later", day: day)
        case "skip": record("skip", day: day)
        case UNNotificationDismissActionIdentifier: record("dismiss", day: day)
        case "open", UNNotificationDefaultActionIdentifier:
            // Only invitations carry a day; other banners just dismiss.
            if !day.isEmpty { record("open", day: day); openAbleton() }
        default: break
        }
        completionHandler()
        finish(0)
    }

    func userNotificationCenter(_ center: UNUserNotificationCenter,
                                willPresent notification: UNNotification,
                                withCompletionHandler completionHandler: @escaping (UNNotificationPresentationOptions) -> Void) {
        completionHandler([.banner, .list])
    }
}

let app = NSApplication.shared
app.setActivationPolicy(.accessory)
let delegate = Delegate()
center.delegate = delegate
center.setNotificationCategories([UNNotificationCategory(
    identifier: "invite",
    actions: [
        UNNotificationAction(identifier: "open", title: "Open Ableton", options: [.foreground]),
        UNNotificationAction(identifier: "later", title: "Later", options: []),
        UNNotificationAction(identifier: "skip", title: "Skip today", options: []),
    ],
    intentIdentifiers: [],
    options: [.customDismissAction])])
app.finishLaunching()

var deadline = Date().addingTimeInterval(12)
if args.count == 2 && args[1] == "--status" {
    center.getNotificationSettings { settings in
        print("authorization=\(settings.authorizationStatus.rawValue) bundle=\(Bundle.main.bundleIdentifier ?? "unknown") icon=\(Bundle.main.object(forInfoDictionaryKey: "CFBundleIconFile") ?? "missing")")
        finish(0)
    }
} else if args.count == 4 || args.count == 6 {
    center.requestAuthorization(options: [.alert]) { granted, error in
        guard granted else { finish(2, error?.localizedDescription ?? "Allow Tracker notifications in System Settings > Notifications."); return }
        let content = UNMutableNotificationContent()
        content.title = args[1]
        content.subtitle = args[2]
        content.body = args[3]
        content.threadIdentifier = "tracker"
        if args.count == 6 {
            content.categoryIdentifier = args[4]
            content.userInfo = ["day": args[5]]
        }
        let request = UNNotificationRequest(identifier: UUID().uuidString, content: content, trigger: nil)
        center.add(request) { error in
            if let error = error { finish(3, error.localizedDescription) }
            else { finish(0) }
        }
    }
} else if args.count == 1 {
    // Relaunched by macOS to deliver a notification response.
    result = 0
    deadline = Date().addingTimeInterval(10)
} else {
    fputs("Usage: tracker-notify title subtitle message [category day] | --status\n", stderr)
    exit(64)
}
while !finished && Date() < deadline {
    RunLoop.main.run(until: Date().addingTimeInterval(0.05))
}
exit(finished || args.count == 1 ? result : 4)
