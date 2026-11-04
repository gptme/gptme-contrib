// One-shot read-only capture. No prompting, focus changes, key events or insertion.
import AppKit
import ApplicationServices
import Foundation

func attribute(_ element: AXUIElement, _ name: String) -> CFTypeRef? {
    var value: CFTypeRef?
    guard AXUIElementCopyAttributeValue(element, name as CFString, &value) == .success else { return nil }
    return value
}

func string(_ element: AXUIElement, _ name: String) -> String? {
    return attribute(element, name) as? String
}

func fail(_ message: String) -> Never {
    FileHandle.standardError.write(Data((message + "\n").utf8))
    exit(1)
}

// Do not trigger a permission dialog or activate any app.
guard AXIsProcessTrusted() else { fail("Accessibility permission is required") }
guard let app = NSWorkspace.shared.frontmostApplication else { fail("No foreground application") }
let application = AXUIElementCreateApplication(app.processIdentifier)
AXUIElementSetMessagingTimeout(application, 2)
guard let raw = attribute(application, kAXFocusedUIElementAttribute) else { fail("No focused element") }
guard CFGetTypeID(raw) == AXUIElementGetTypeID() else { fail("Invalid focused element") }
let field = unsafeBitCast(raw, to: AXUIElement.self)
AXUIElementSetMessagingTimeout(field, 2)
let role = string(field, kAXRoleAttribute) ?? ""
let subrole = string(field, kAXSubroleAttribute) ?? ""
// Secure fields are refused before reading any value or surrounding text.
guard role != "AXSecureTextField" && subrole != "AXSecureTextField" else { fail("Secure field refused") }
guard ["AXTextField", "AXTextArea", "AXComboBox"].contains(role) else { fail("Unsupported focused role") }
var writable: DarwinBoolean = false
let writableStatus = AXUIElementIsAttributeSettable(field, kAXValueAttribute as CFString, &writable)
guard writableStatus == .success && writable.boolValue else { fail("Field is not writable") }
guard let text = string(field, kAXValueAttribute) else { fail("No accessible field value") }
guard text.utf8.count <= 20000 else { fail("Field too large for bounded capture") }
var rangeJSON: Any = NSNull()
if let rawRange = attribute(field, kAXSelectedTextRangeAttribute), CFGetTypeID(rawRange) == AXValueGetTypeID() {
    let axRange = unsafeBitCast(rawRange, to: AXValue.self)
    var range = CFRange()
    if AXValueGetType(axRange) == .cfRange && AXValueGetValue(axRange, .cfRange, &range) {
        rangeJSON = ["location": range.location, "length": range.length]
    }
}
var windowTitle: Any = NSNull()
if let rawWindow = attribute(application, kAXFocusedWindowAttribute), CFGetTypeID(rawWindow) == AXUIElementGetTypeID() {
    let window = unsafeBitCast(rawWindow, to: AXUIElement.self)
    windowTitle = string(window, kAXTitleAttribute) as Any? ?? NSNull()
}
// Reject app/field switches during the read instead of mixing two targets.
guard NSWorkspace.shared.frontmostApplication?.processIdentifier == app.processIdentifier,
      let after = attribute(application, kAXFocusedUIElementAttribute), CFEqual(raw, after) else {
    fail("Focus changed during capture")
}
let result: [String: Any] = [
    "schema_version": 1,
    "captured_at": ISO8601DateFormatter().string(from: Date()),
    "platform": "macos",
    "pid": Int(app.processIdentifier),
    "bundle_id": app.bundleIdentifier as Any? ?? NSNull(),
    "app": app.localizedName as Any? ?? NSNull(),
    "window_title": windowTitle,
    "role": role,
    "subrole": subrole,
    "identifier": string(field, kAXIdentifierAttribute) as Any? ?? NSNull(),
    "text": text,
    "selection": rangeJSON,
    "selected_text": string(field, kAXSelectedTextAttribute) as Any? ?? NSNull(),
    "secure": false,
    "editable": true,
    "coverage": "Focused field only; surrounding conversation comes from historical observations and may be incomplete. Not an insertion handle."
]
let data = try JSONSerialization.data(withJSONObject: result, options: [.prettyPrinted, .sortedKeys])
FileHandle.standardOutput.write(data)
FileHandle.standardOutput.write(Data("\n".utf8))
