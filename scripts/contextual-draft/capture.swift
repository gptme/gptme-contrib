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

func fieldText(_ element: AXUIElement) -> String? {
    if let value = string(element, kAXValueAttribute) { return value }
    // Notes exposes its editor through AXStringForRange instead of AXValue.
    // AX character offsets are UTF-16 units; bound before requesting the text.
    guard let count = attribute(element, kAXNumberOfCharactersAttribute) as? NSNumber,
          count.intValue >= 0, count.intValue <= 20000 else { return nil }
    var range = CFRange(location: 0, length: count.intValue)
    guard let parameter = AXValueCreate(.cfRange, &range) else { return nil }
    var value: CFTypeRef?
    guard AXUIElementCopyParameterizedAttributeValue(
        element, kAXStringForRangeParameterizedAttribute as CFString,
        parameter, &value
    ) == .success else { return nil }
    return value as? String
}

func fail(_ message: String) -> Never {
    FileHandle.standardError.write(Data((message + "\n").utf8))
    exit(1)
}

func selectedRange(_ element: AXUIElement) -> [String: Int]? {
    guard let raw = attribute(element, kAXSelectedTextRangeAttribute),
          CFGetTypeID(raw) == AXValueGetTypeID() else { return nil }
    let value = unsafeBitCast(raw, to: AXValue.self)
    var range = CFRange()
    guard AXValueGetType(value) == .cfRange,
          AXValueGetValue(value, .cfRange, &range) else { return nil }
    return ["location": range.location, "length": range.length]
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
guard let text = fieldText(field) else { fail("No accessible field value") }
guard text.utf8.count <= 20000 else { fail("Field too large for bounded capture") }
let range = selectedRange(field)
let selectedText = string(field, kAXSelectedTextAttribute)
let identifier = string(field, kAXIdentifierAttribute)
var windowTitle: Any = NSNull()
if let rawWindow = attribute(application, kAXFocusedWindowAttribute), CFGetTypeID(rawWindow) == AXUIElementGetTypeID() {
    let window = unsafeBitCast(rawWindow, to: AXUIElement.self)
    windowTitle = string(window, kAXTitleAttribute) as Any? ?? NSNull()
}
// Re-read field content and selection as well as identity. AX has no atomic
// snapshot transaction; this detects observed changes, not every possible ABA.
guard fieldText(field) == text,
      selectedRange(field) == range,
      string(field, kAXSelectedTextAttribute) == selectedText else {
    fail("Field text or selection changed during capture")
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
    "identifier": identifier as Any? ?? NSNull(),
    "text": text,
    "selection": range as Any? ?? NSNull(),
    "selected_text": selectedText as Any? ?? NSNull(),
    "secure": false,
    "editable": true,
    "coverage": "Focused field only; surrounding conversation comes from historical observations and may be incomplete. Not an insertion handle."
]
let interactive = CommandLine.arguments.contains("--insert-session")
let data = try JSONSerialization.data(withJSONObject: result, options: interactive ? [.sortedKeys] : [.prettyPrinted, .sortedKeys])
FileHandle.standardOutput.write(data)
FileHandle.standardOutput.write(Data("\n".utf8))
if interactive {
    // Keep the actual AX element alive across generation; metadata is not a handle.
    // Selected-text rewriting is intentionally not part of this draft action.
    guard let originalRange = range, originalRange["length"] == 0 else {
        fail("Direct drafting requires a cursor, not a text selection")
    }
    guard let line = readLine(), line.utf8.count <= 100000,
          let payload = line.data(using: .utf8),
          let command = try? JSONSerialization.jsonObject(with: payload) as? [String: String],
          let draft = command["draft"], !draft.isEmpty else { fail("No draft to insert") }
    guard NSWorkspace.shared.frontmostApplication?.processIdentifier == app.processIdentifier,
          let current = attribute(application, kAXFocusedUIElementAttribute), CFEqual(raw, current),
          fieldText(field) == text, selectedRange(field) == range,
          string(field, kAXSelectedTextAttribute) == selectedText else {
        fail("Target changed; draft retained but not inserted")
    }
    var canInsert: DarwinBoolean = false
    guard AXUIElementIsAttributeSettable(field, kAXSelectedTextAttribute as CFString, &canInsert) == .success,
          canInsert.boolValue else { fail("Target does not support selected-text insertion") }
    // One targeted AX write. No clipboard, keyboard simulation, focus activation,
    // whole-field replacement, Return, or fallback into another control.
    guard AXUIElementSetAttributeValue(field, kAXSelectedTextAttribute as CFString, draft as CFString) == .success else {
        fail("Native insertion failed; no keyboard fallback attempted")
    }
    FileHandle.standardOutput.write(Data("{\"inserted\":true}\n".utf8))
}
