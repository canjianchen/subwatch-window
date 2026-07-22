// ocr_helper.swift — local OCR using Apple's Vision framework.
// Usage: ocr_helper <image_path>
// Output: JSON array of recognized lines: [{ "text", "confidence", "x", "y", "w", "h" }]
// Coordinates are normalized (0..1), origin bottom-left (Vision convention).
// English + Chinese recognition is enabled. No network, no third-party installs.

import Foundation
import Vision
import AppKit

func fail(_ message: String) -> Never {
    FileHandle.standardError.write((message + "\n").data(using: .utf8)!)
    print("[]")
    exit(1)
}

guard CommandLine.arguments.count >= 2 else {
    fail("usage: ocr_helper <image_path>")
}

let imagePath = CommandLine.arguments[1]
guard let nsImage = NSImage(contentsOfFile: imagePath),
      let cgImage = nsImage.cgImage(forProposedRect: nil, context: nil, hints: nil) else {
    fail("could not load image: \(imagePath)")
}

let request = VNRecognizeTextRequest()
request.recognitionLevel = .accurate
request.usesLanguageCorrection = true
// Language order matters for Vision: listing a CJK language FIRST is required
// or Chinese is silently dropped when English leads. This ordering reliably
// captures both English and Chinese in the same mixed frame.
request.recognitionLanguages = ["zh-Hans", "zh-Hant", "en-US"]

let handler = VNImageRequestHandler(cgImage: cgImage, options: [:])
do {
    try handler.perform([request])
} catch {
    fail("OCR failed: \(error.localizedDescription)")
}

var results: [[String: Any]] = []
for observation in (request.results ?? []) {
    guard let candidate = observation.topCandidates(1).first else { continue }
    let box = observation.boundingBox
    results.append([
        "text": candidate.string,
        "confidence": candidate.confidence,
        "x": box.origin.x,
        "y": box.origin.y,
        "w": box.size.width,
        "h": box.size.height,
    ])
}

if let data = try? JSONSerialization.data(withJSONObject: results, options: []),
   let json = String(data: data, encoding: .utf8) {
    print(json)
} else {
    print("[]")
}
