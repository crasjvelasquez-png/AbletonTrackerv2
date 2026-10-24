// A native app identity gives macOS Tracker's name and icon, rather than Python's.
import AppKit
import UserNotifications

let args = CommandLine.arguments
let center = UNUserNotificationCenter.current()
var finished = false
var result: Int32 = 1
func finish(_ code: Int32, _ message: String = "") {
    DispatchQueue.main.async {
        if !message.isEmpty { fputs(message + "\n", stderr) }
        result = code
        finished = true
    }
}
if args.count == 2 && args[1] == "--status" {
    center.getNotificationSettings { settings in
        print("authorization=\(settings.authorizationStatus.rawValue) bundle=\(Bundle.main.bundleIdentifier ?? "unknown") icon=\(Bundle.main.object(forInfoDictionaryKey: "CFBundleIconFile") ?? "missing")")
        finish(0)
    }
} else if args.count == 4 {
    center.requestAuthorization(options: [.alert]) { granted, error in
        guard granted else { finish(2, error?.localizedDescription ?? "Allow Tracker notifications in System Settings > Notifications."); return }
        let content = UNMutableNotificationContent()
        content.title = args[1]
        content.subtitle = args[2]
        content.body = args[3]
        content.threadIdentifier = "tracker"
        let request = UNNotificationRequest(identifier: UUID().uuidString, content: content, trigger: nil)
        center.add(request) { error in
            if let error = error { finish(3, error.localizedDescription) }
            else { finish(0) }
        }
    }
} else {
    fputs("Usage: tracker-notify title subtitle message | --status\n", stderr)
    exit(64)
}
let deadline = Date().addingTimeInterval(12)
while !finished && Date() < deadline {
    RunLoop.main.run(until: Date().addingTimeInterval(0.05))
}
exit(finished ? result : 4)
