import AVFoundation
import CoreMedia
import Foundation
import ScreenCaptureKit

// Only Live's output is inspected. No microphone, video output or recording.
final class AudioProbe: NSObject, SCStreamOutput {
    var peak: Float = 0
    var sumSquares: Double = 0
    var sampleCount = 0
    var invalidFormat = false

    func stream(_ stream: SCStream, didOutputSampleBuffer sb: CMSampleBuffer, of type: SCStreamOutputType) {
        guard type == .audio, sb.isValid,
              let description = CMSampleBufferGetFormatDescription(sb),
              let format = CMAudioFormatDescriptionGetStreamBasicDescription(description)?.pointee,
              format.mFormatID == kAudioFormatLinearPCM,
              format.mBitsPerChannel == 32,
              (format.mFormatFlags & kAudioFormatFlagIsFloat) != 0 else {
            invalidFormat = true
            return
        }
        var required = 0
        var block: CMBlockBuffer?
        let sizing = CMSampleBufferGetAudioBufferListWithRetainedBlockBuffer(
            sb, bufferListSizeNeededOut: &required, bufferListOut: nil,
            bufferListSize: 0, blockBufferAllocator: kCFAllocatorDefault,
            blockBufferMemoryAllocator: kCFAllocatorDefault, flags: 0,
            blockBufferOut: &block)
        guard required >= MemoryLayout<AudioBufferList>.size,
              sizing == noErr || sizing == kCMSampleBufferError_ArrayTooSmall else { return }
        let storage = UnsafeMutableRawPointer.allocate(byteCount: required, alignment: 16)
        defer { storage.deallocate() }
        let list = storage.bindMemory(to: AudioBufferList.self, capacity: 1)
        let status = CMSampleBufferGetAudioBufferListWithRetainedBlockBuffer(
            sb, bufferListSizeNeededOut: nil, bufferListOut: list,
            bufferListSize: required, blockBufferAllocator: kCFAllocatorDefault,
            blockBufferMemoryAllocator: kCFAllocatorDefault, flags: 0,
            blockBufferOut: &block)
        guard status == noErr else { return }
        for buffer in UnsafeMutableAudioBufferListPointer(list) {
            guard let data = buffer.mData else { continue }
            let count = Int(buffer.mDataByteSize) / MemoryLayout<Float>.size
            let samples = data.bindMemory(to: Float.self, capacity: count)
            for i in 0..<count {
                let sample = samples[i]
                guard sample.isFinite else { continue }
                peak = max(peak, abs(sample))
                sumSquares += Double(sample) * Double(sample)
                sampleCount += 1
            }
        }
    }

    /// One status line for the samples since the last report, then reset.
    func report(threshold: Double) -> String {
        defer { peak = 0; sumSquares = 0; sampleCount = 0; invalidFormat = false }
        if invalidFormat || sampleCount == 0 { return "unavailable samples=\(sampleCount)" }
        let rms = sqrt(sumSquares / Double(sampleCount))
        let active = rms >= threshold || Double(peak) >= threshold
        return "\(active ? "active" : "quiet") rms=\(rms) peak=\(peak) samples=\(sampleCount)"
    }
}

final class StopWatcher: NSObject, SCStreamDelegate {
    func stream(_ stream: SCStream, didStopWithError error: Error) {
        print("unavailable stream_stopped \(error.localizedDescription)")
        exit(1)
    }
}

let args = CommandLine.arguments
let duration = args.count > 1 ? (Double(args[1]) ?? 1) : 1
let threshold = args.count > 2 ? (Double(args[2]) ?? 0.000316) : 0.000316
let pid = args.count > 3 ? (Int32(args[3]) ?? 0) : 0
// "stream" keeps one capture open and reports every `duration` seconds, so the
// tracker does not set up and tear down a capture on Live every few seconds.
let streaming = args.count > 4 && args[4] == "stream"
setvbuf(stdout, nil, _IOLBF, 0)
if streaming {
    // The tracker holds stdin open; EOF means it exited, so stop capturing.
    Thread.detachNewThread {
        _ = FileHandle.standardInput.readDataToEndOfFile()
        exit(0)
    }
}
// Only applications and displays are needed; skipping off-screen and desktop
// windows keeps the WindowServer enumeration small.
var content = try await SCShareableContent.excludingDesktopWindows(true, onScreenWindowsOnly: true)
if pid > 0, !content.applications.contains(where: { $0.processID == pid }) {
    // Live can be frontmost with its window minimized; look once more widely.
    content = try await SCShareableContent.excludingDesktopWindows(true, onScreenWindowsOnly: false)
}
guard pid > 0, let app = content.applications.first(where: { $0.processID == pid }),
      let display = content.displays.first else {
    print("unavailable live_or_display_missing")
    exit(0)
}
let filter = SCContentFilter(display: display, including: [app], exceptingWindows: [])
let config = SCStreamConfiguration()
config.capturesAudio = true
config.excludesCurrentProcessAudio = true
config.width = 2
config.height = 2
config.minimumFrameInterval = CMTime(value: 1, timescale: 1)
config.queueDepth = 3
let probe = AudioProbe()
let watcher = StopWatcher()
let queue = DispatchQueue(label: "tracker.live-audio")
let stream = SCStream(filter: filter, configuration: config, delegate: watcher)
try stream.addStreamOutput(probe, type: .audio, sampleHandlerQueue: queue)
try await withCheckedThrowingContinuation { (continuation: CheckedContinuation<Void, Error>) in
    stream.startCapture(completionHandler: { error in
        if let error = error { continuation.resume(throwing: error) }
        else { continuation.resume() }
    })
}
let window = UInt64(max(0.1, min(duration, 2)) * 1_000_000_000)
if streaming {
    while true {
        try await Task.sleep(nanoseconds: window)
        print(queue.sync { probe.report(threshold: threshold) })
    }
}
try await Task.sleep(nanoseconds: window)
try await withCheckedThrowingContinuation { (continuation: CheckedContinuation<Void, Error>) in
    stream.stopCapture(completionHandler: { error in
        if let error = error { continuation.resume(throwing: error) }
        else { continuation.resume() }
    })
}
print(queue.sync { probe.report(threshold: threshold) })
