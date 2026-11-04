// Non-activating, click-through activity indicator. Stdin EOF ends its lifetime.
// No AX access, focus activation, captured text, clipboard, or model calls.
import AppKit
import Foundation

let app = NSApplication.shared
app.setActivationPolicy(.accessory)
let width: CGFloat = 128
let height: CGFloat = 30
let panel = NSPanel(contentRect: NSRect(x: 0, y: 0, width: width, height: height),
                    styleMask: [.borderless, .nonactivatingPanel],
                    backing: .buffered, defer: false)
panel.isOpaque = false
panel.backgroundColor = .clear
panel.hasShadow = true
panel.ignoresMouseEvents = true
panel.level = .floating
panel.collectionBehavior = [.canJoinAllSpaces, .fullScreenAuxiliary]
let background = NSVisualEffectView(frame: NSRect(x: 0, y: 0, width: width, height: height))
background.material = .hudWindow
background.blendingMode = .behindWindow
background.state = .active
background.wantsLayer = true
background.layer?.cornerRadius = 8
let spinner = NSProgressIndicator(frame: NSRect(x: 9, y: 7, width: 16, height: 16))
spinner.style = .spinning
spinner.controlSize = .small
spinner.isIndeterminate = true
spinner.startAnimation(nil)
background.addSubview(spinner)
let label = NSTextField(labelWithString: "Drafting…")
label.font = .systemFont(ofSize: 12, weight: .medium)
label.frame = NSRect(x: 32, y: 6, width: 90, height: 18)
background.addSubview(label)
panel.contentView = background
// AX coordinates use a top-left origin; AppKit uses the primary display bottom-left.
let primaryTop = NSScreen.screens.first?.frame.maxY ?? 0
var point = NSPoint(x: 20, y: primaryTop - height - 50)
if CommandLine.arguments.count == 3,
   let x = Double(CommandLine.arguments[1]), let y = Double(CommandLine.arguments[2]),
   x.isFinite, y.isFinite {
    point = NSPoint(x: CGFloat(x) + 8, y: primaryTop - CGFloat(y) - height - 6)
}
let bounds = NSScreen.screens.first(where: { $0.frame.contains(point) })?.visibleFrame
    ?? NSScreen.main?.visibleFrame ?? .zero
point.x = min(max(point.x, bounds.minX), max(bounds.minX, bounds.maxX - width))
point.y = min(max(point.y, bounds.minY), max(bounds.minY, bounds.maxY - height))
panel.setFrameOrigin(point)
panel.orderFrontRegardless() // Non-activating panel; never makeKey or activate.
DispatchQueue.global().async {
    _ = FileHandle.standardInput.readDataToEndOfFile()
    DispatchQueue.main.async { app.terminate(nil) }
}
// Fail-safe if the controlling process stalls without closing stdin.
Timer.scheduledTimer(withTimeInterval: 210, repeats: false) { _ in app.terminate(nil) }
app.run()
