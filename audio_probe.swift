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
}

let args = CommandLine.arguments
let duration = args.count > 1 ? (Double(args[1]) ?? 1) : 1
let threshold = args.count > 2 ? (Double(args[2]) ?? 0.000316) : 0.000316
let pid = args.count > 3 ? (Int32(args[3]) ?? 0) : 0
let content = try await SCShareableContent.excludingDesktopWindows(false, onScreenWindowsOnly: false)
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
let probe = AudioProbe()
let queue = DispatchQueue(label: "tracker.live-audio")
let stream = SCStream(filter: filter, configuration: config, delegate: nil)
try stream.addStreamOutput(probe, type: .audio, sampleHandlerQueue: queue)
try await withCheckedThrowingContinuation { (continuation: CheckedContinuation<Void, Error>) in
    stream.startCapture(completionHandler: { error in
        if let error = error { continuation.resume(throwing: error) }
        else { continuation.resume() }
    })
}
try await Task.sleep(nanoseconds: UInt64(max(0.1, min(duration, 2)) * 1_000_000_000))
try await withCheckedThrowingContinuation { (continuation: CheckedContinuation<Void, Error>) in
    stream.stopCapture(completionHandler: { error in
        if let error = error { continuation.resume(throwing: error) }
        else { continuation.resume() }
    })
}
queue.sync {
    if probe.invalidFormat || probe.sampleCount == 0 {
        print("unavailable samples=\(probe.sampleCount)")
    } else {
        let rms = sqrt(probe.sumSquares / Double(probe.sampleCount))
        let active = rms >= threshold || Double(probe.peak) >= threshold
        print("\(active ? "active" : "quiet") rms=\(rms) peak=\(probe.peak) samples=\(probe.sampleCount)")
    }
}
