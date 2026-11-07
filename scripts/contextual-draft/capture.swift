// One-shot read-only capture. No prompting, focus changes, key events or insertion.
import AppKit
import ApplicationServices
import Foundation

func attribute(_ element: AXUIElement, _ name: String) -> CFTypeRef? {
    var value: CFTypeRef?
    guard AXUIElementCopyAttributeValue(element, name as CFString, &value) == .success else { return nil }
    return value
}

// Optional, non-security metadata only (identifier, window title). A failed
// read omits metadata; it never feeds a security or change-detection decision.
func string(_ element: AXUIElement, _ name: String) -> String? {
    return attribute(element, name) as? String
}

// Tri-state AX read. Only an explicit "no value"/"unsupported attribute" answer
// means absent; every other error (timeout, cannot complete, API disabled,
// invalid element, unexpected type) is a failure, never a benign default.
enum Read<T: Equatable>: Equatable {
    case present(T)
    case absent
    case failed
}

func classify<T: Equatable>(_ status: AXError, _ value: T?, hasValue: Bool) -> Read<T> {
    switch status {
    case .success:
        if let value { return .present(value) }
        return hasValue ? .failed : .absent
    case .noValue, .attributeUnsupported:
        return .absent
    default:
        return .failed
    }
}

func readString(_ element: AXUIElement, _ name: String) -> Read<String> {
    var value: CFTypeRef?
    let status = AXUIElementCopyAttributeValue(element, name as CFString, &value)
    return classify(status, value as? String, hasValue: value != nil)
}

// Change detection: equal successful-or-absent reads only. Two failed reads
// are not evidence that nothing changed.
func stable<T: Equatable>(_ before: Read<T>, _ after: Read<T>) -> Bool {
    if case .failed = before { return false }
    return before == after
}

enum Security: Equatable {
    case allowed(role: String, subrole: String)
    case refused(String)
}

// Fail closed: the value is read only after role and subrole were both read
// successfully (a subrole may be explicitly absent) and neither marks the
// field secure. Any read failure refuses the field.
func securityDecision(role: Read<String>, subrole: Read<String>) -> Security {
    guard case .present(let roleValue) = role else {
        return .refused("Cannot verify field role; refused")
    }
    let subroleValue: String
    switch subrole {
    case .present(let value): subroleValue = value
    case .absent: subroleValue = ""
    case .failed: return .refused("Cannot verify field security; refused")
    }
    if roleValue == "AXSecureTextField" || subroleValue == "AXSecureTextField" {
        return .refused("Secure field refused")
    }
    guard ["AXTextField", "AXTextArea", "AXComboBox"].contains(roleValue) else {
        return .refused("Unsupported focused role")
    }
    return .allowed(role: roleValue, subrole: subroleValue)
}

func fieldSecurity(_ element: AXUIElement) -> Security {
    return securityDecision(role: readString(element, kAXRoleAttribute),
                            subrole: readString(element, kAXSubroleAttribute))
}

// Limits shared with contextual_draft.py (MAX_TARGET_TEXT_JSON, MAX_PROTOCOL_LINE).
let maxTextJSON = 20000
let maxLine = 100000
let maxMetadataBytes = 1000

// Size of a string once JSON-encoded, as it travels in the protocol line.
func jsonEncodedSize(_ text: String) -> Int? {
    guard let data = try? JSONSerialization.data(withJSONObject: [text]) else { return nil }
    return data.count - 4  // ["..."]
}

func boundedMetadata(_ value: String?) -> Any {
    guard let value else { return NSNull() }
    return String(decoding: value.utf8.prefix(maxMetadataBytes), as: UTF8.self)
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

func selectedRange(_ element: AXUIElement) -> Read<[String: Int]> {
    var raw: CFTypeRef?
    let status = AXUIElementCopyAttributeValue(element, kAXSelectedTextRangeAttribute as CFString, &raw)
    var parsed: [String: Int]?
    if let raw, CFGetTypeID(raw) == AXValueGetTypeID() {
        let value = unsafeBitCast(raw, to: AXValue.self)
        var range = CFRange()
        if AXValueGetType(value) == .cfRange, AXValueGetValue(value, .cfRange, &range) {
            parsed = ["location": range.location, "length": range.length]
        }
    }
    return classify(status, parsed, hasValue: raw != nil)
}

func selfTest() -> Never {
    func check(_ condition: Bool, _ name: String) {
        if !condition { fail("self-test failed: \(name)") }
    }
    let field: Read<String> = .present("AXTextField")
    check(securityDecision(role: field, subrole: .failed) == .refused("Cannot verify field security; refused"),
          "subrole read failure refuses")
    check(securityDecision(role: .failed, subrole: .absent) == .refused("Cannot verify field role; refused"),
          "role read failure refuses")
    check(securityDecision(role: .absent, subrole: .absent) == .refused("Cannot verify field role; refused"),
          "absent role refuses")
    check(securityDecision(role: field, subrole: .present("AXSecureTextField")) == .refused("Secure field refused"),
          "secure subrole refuses")
    check(securityDecision(role: .present("AXSecureTextField"), subrole: .absent) == .refused("Secure field refused"),
          "secure role refuses")
    check(securityDecision(role: .present("AXButton"), subrole: .absent) == .refused("Unsupported focused role"),
          "unsupported role refuses")
    check(securityDecision(role: .present("AXTextArea"), subrole: .absent) == .allowed(role: "AXTextArea", subrole: ""),
          "plain text area allowed")
    let errors: [AXError] = [.cannotComplete, .failure, .apiDisabled, .invalidUIElement,
                             .notImplemented, .illegalArgument]
    for error in errors {
        check(classify(error, nil as String?, hasValue: false) == .failed, "error \(error.rawValue) is failure")
    }
    check(classify(.noValue, nil as String?, hasValue: false) == .absent, "noValue is absent")
    check(classify(.attributeUnsupported, nil as String?, hasValue: false) == .absent, "unsupported is absent")
    check(classify(.success, nil as String?, hasValue: true) == .failed, "unexpected type is failure")
    check(classify(.success, "x", hasValue: true) == .present("x"), "success is present")
    check(!stable(Read<String>.failed, .failed), "failed reads are not stable")
    check(stable(Read<String>.absent, .absent), "absent reads are stable")
    check(!stable(Read<String>.present("a"), .present("b")), "changed reads are not stable")
    check(jsonEncodedSize(String(repeating: "\u{1}", count: 3)) == 18, "control characters count as escapes")
    check(jsonEncodedSize("abc") == 3, "plain text counts as bytes")
    print("capture self-test ok")
    exit(0)
}

if CommandLine.arguments.contains("--self-test") { selfTest() }

// Do not trigger a permission dialog or activate any app.
guard AXIsProcessTrusted() else { fail("Accessibility permission is required") }
guard let app = NSWorkspace.shared.frontmostApplication else { fail("No foreground application") }
let application = AXUIElementCreateApplication(app.processIdentifier)
AXUIElementSetMessagingTimeout(application, 2)
guard let raw = attribute(application, kAXFocusedUIElementAttribute) else { fail("No focused element") }
guard CFGetTypeID(raw) == AXUIElementGetTypeID() else { fail("Invalid focused element") }
let field = unsafeBitCast(raw, to: AXUIElement.self)
AXUIElementSetMessagingTimeout(field, 2)
// Secure fields are refused before reading any value or surrounding text, and
// so is any field whose security cannot be established.
let security = fieldSecurity(field)
guard case .allowed(let role, let subrole) = security else {
    if case .refused(let reason) = security { fail(reason) }
    fail("Cannot verify field security; refused")
}
var writable: DarwinBoolean = false
let writableStatus = AXUIElementIsAttributeSettable(field, kAXValueAttribute as CFString, &writable)
guard writableStatus == .success && writable.boolValue else { fail("Field is not writable") }
guard let text = fieldText(field) else { fail("No accessible field value") }
guard let textSize = jsonEncodedSize(text), textSize <= maxTextJSON else {
    fail("Field too large for bounded capture")
}
let range = selectedRange(field)
let selectedText = readString(field, kAXSelectedTextAttribute)
guard range != .failed, selectedText != .failed else { fail("Cannot read field selection") }
let identifier = string(field, kAXIdentifierAttribute)
var windowTitle: Any = NSNull()
if let rawWindow = attribute(application, kAXFocusedWindowAttribute), CFGetTypeID(rawWindow) == AXUIElementGetTypeID() {
    let window = unsafeBitCast(rawWindow, to: AXUIElement.self)
    AXUIElementSetMessagingTimeout(window, 2)
    windowTitle = boundedMetadata(string(window, kAXTitleAttribute))
}
// Re-read security, content and selection as well as identity. AX has no atomic
// snapshot transaction; this detects observed changes, not every possible ABA.
guard fieldSecurity(field) == security,
      fieldText(field) == text,
      stable(range, selectedRange(field)),
      stable(selectedText, readString(field, kAXSelectedTextAttribute)) else {
    fail("Field text or selection changed during capture")
}
// Reject app/field switches during the read instead of mixing two targets.
guard NSWorkspace.shared.frontmostApplication?.processIdentifier == app.processIdentifier,
      let after = attribute(application, kAXFocusedUIElementAttribute), CFEqual(raw, after) else {
    fail("Focus changed during capture")
}
var fieldBounds: Any = NSNull()
if let rawPosition = attribute(field, kAXPositionAttribute),
   let rawSize = attribute(field, kAXSizeAttribute),
   CFGetTypeID(rawPosition) == AXValueGetTypeID(),
   CFGetTypeID(rawSize) == AXValueGetTypeID() {
    var position = CGPoint.zero
    var size = CGSize.zero
    if AXValueGetValue(unsafeBitCast(rawPosition, to: AXValue.self), .cgPoint, &position),
       AXValueGetValue(unsafeBitCast(rawSize, to: AXValue.self), .cgSize, &size) {
        fieldBounds = ["x": position.x, "y": position.y,
                       "width": size.width, "height": size.height]
    }
}
let result: [String: Any] = [
    "field_bounds": fieldBounds,
    "schema_version": 1,
    "captured_at": ISO8601DateFormatter().string(from: Date()),
    "platform": "macos",
    "pid": Int(app.processIdentifier),
    "bundle_id": boundedMetadata(app.bundleIdentifier),
    "app": boundedMetadata(app.localizedName),
    "window_title": windowTitle,
    "role": role,
    "subrole": subrole,
    "identifier": boundedMetadata(identifier),
    "text": text,
    "selection": { if case .present(let value) = range { return value as Any }; return NSNull() }(),
    "selected_text": { if case .present(let value) = selectedText { return value as Any }; return NSNull() }(),
    "secure": false,
    "editable": true,
    "coverage": "Focused field only; surrounding conversation comes from historical observations and may be incomplete. Not an insertion handle."
]
let interactive = CommandLine.arguments.contains("--insert-session")
let data = try JSONSerialization.data(withJSONObject: result, options: interactive ? [.sortedKeys] : [.prettyPrinted, .sortedKeys])
// Same limit as the reader (contextual_draft.MAX_PROTOCOL_LINE); with the
// encoded text and metadata bounds above, a valid capture always fits.
guard data.count <= maxLine else { fail("Capture exceeds protocol line limit") }
FileHandle.standardOutput.write(data)
FileHandle.standardOutput.write(Data("\n".utf8))
if interactive {
    // Keep the actual AX element alive across generation; metadata is not a handle.
    // Selected-text rewriting is intentionally not part of this draft action.
    guard case .present(let originalRange) = range, originalRange["length"] == 0 else {
        fail("Direct drafting requires a cursor, not a text selection")
    }
    guard let line = readLine(), line.utf8.count <= maxLine,
          let payload = line.data(using: .utf8),
          let command = try? JSONSerialization.jsonObject(with: payload) as? [String: Any],
          let draft = command["draft"] as? String, !draft.isEmpty,
          let budget = (command["budget_s"] as? NSNumber)?.doubleValue, budget > 0 else {
        fail("No draft to insert")
    }
    // Insertion budget from the launcher, strictly inside its reply deadline.
    let received = Date()
    guard NSWorkspace.shared.frontmostApplication?.processIdentifier == app.processIdentifier,
          let current = attribute(application, kAXFocusedUIElementAttribute), CFEqual(raw, current),
          fieldSecurity(field) == security,
          fieldText(field) == text, stable(range, selectedRange(field)),
          stable(selectedText, readString(field, kAXSelectedTextAttribute)) else {
        fail("Target changed; draft retained but not inserted")
    }
    var canInsert: DarwinBoolean = false
    guard AXUIElementIsAttributeSettable(field, kAXSelectedTextAttribute as CFString, &canInsert) == .success,
          canInsert.boolValue else { fail("Target does not support selected-text insertion") }
    // Revalidation can be slow; never write once the launcher may have given up.
    guard Date().timeIntervalSince(received) <= budget else {
        fail("Insertion budget exceeded; draft retained but not inserted")
    }
    // One targeted AX write. No clipboard, keyboard simulation, focus activation,
    // whole-field replacement, Return, or fallback into another control.
    guard AXUIElementSetAttributeValue(field, kAXSelectedTextAttribute as CFString, draft as CFString) == .success else {
        fail("Native insertion failed; no keyboard fallback attempted")
    }
    FileHandle.standardOutput.write(Data("{\"inserted\":true}\n".utf8))
}
