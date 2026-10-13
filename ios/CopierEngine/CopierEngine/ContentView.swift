import SwiftUI
import UIKit
import WebKit

private let appURL = URL(string: "https://copier-engine.gimmholding.com")!

struct ContentView: View {
    @State private var isLoading = true
    @State private var failed = false
    @State private var reload: (() -> Void)?

    var body: some View {
        ZStack {
            Color(red: 14 / 255, green: 16 / 255, blue: 24 / 255)
                .ignoresSafeArea()

            CopierWebView(url: appURL, isLoading: $isLoading, failed: $failed, reload: $reload)
                .ignoresSafeArea(edges: .bottom)

            if isLoading {
                VStack(spacing: 16) {
                    Image(systemName: "chart.xyaxis.line")
                        .font(.system(size: 32, weight: .medium))
                        .foregroundStyle(Color(red: 0.67, green: 0.62, blue: 1))
                    ProgressView().tint(.white.opacity(0.8))
                    Text("Opening your private workspace")
                        .font(.system(size: 14, weight: .medium))
                        .foregroundStyle(.white.opacity(0.72))
                }
                .padding(28)
                .background(.ultraThinMaterial, in: RoundedRectangle(cornerRadius: 20))
                .transition(.opacity)
            }

            if failed && !isLoading {
                VStack(spacing: 14) {
                    Image(systemName: "wifi.exclamationmark")
                        .font(.system(size: 30))
                        .foregroundStyle(Color(red: 0.67, green: 0.62, blue: 1))
                    Text("Workspace unavailable")
                        .font(.system(size: 18, weight: .semibold))
                        .foregroundStyle(.white)
                    Text("Check your connection and try again.")
                        .font(.system(size: 13))
                        .foregroundStyle(.white.opacity(0.65))
                    Button("Try again") { reload?() }
                        .font(.system(size: 14, weight: .semibold))
                        .padding(.horizontal, 20).padding(.vertical, 11)
                        .background(Color(red: 0.48, green: 0.42, blue: 0.88), in: Capsule())
                        .foregroundStyle(.white)
                }
                .padding(28)
                .frame(maxWidth: .infinity, maxHeight: .infinity)
                .background(Color(red: 14 / 255, green: 16 / 255, blue: 24 / 255))
            }
        }
        .animation(.easeInOut(duration: 0.2), value: isLoading)
        .statusBarHidden(false)
    }
}

private struct CopierWebView: UIViewRepresentable {
    let url: URL
    @Binding var isLoading: Bool
    @Binding var failed: Bool
    @Binding var reload: (() -> Void)?

    func makeCoordinator() -> Coordinator { Coordinator(parent: self) }

    func makeUIView(context: Context) -> WKWebView {
        let configuration = WKWebViewConfiguration()
        // The default data store is persistent, so the server's HttpOnly login cookie
        // survives app restarts without putting credentials in native storage.
        let webView = WKWebView(frame: .zero, configuration: configuration)
        webView.navigationDelegate = context.coordinator
        webView.uiDelegate = context.coordinator
        webView.allowsBackForwardNavigationGestures = true
        webView.isOpaque = false
        webView.backgroundColor = UIColor(red: 14/255, green: 16/255, blue: 24/255, alpha: 1)
        webView.scrollView.backgroundColor = webView.backgroundColor
        reload = { [weak webView] in
            failed = false
            isLoading = true
            webView?.load(URLRequest(url: url, cachePolicy: .reloadIgnoringLocalCacheData))
        }
        webView.load(URLRequest(url: url))
        return webView
    }

    func updateUIView(_ webView: WKWebView, context: Context) {}

    final class Coordinator: NSObject, WKNavigationDelegate, WKUIDelegate {
        var parent: CopierWebView
        init(parent: CopierWebView) { self.parent = parent }

        func webView(_ webView: WKWebView, didStartProvisionalNavigation navigation: WKNavigation!) {
            DispatchQueue.main.async { self.parent.isLoading = true; self.parent.failed = false }
        }
        func webView(_ webView: WKWebView, didFinish navigation: WKNavigation!) {
            DispatchQueue.main.async { self.parent.isLoading = false; self.parent.failed = false }
        }
        func webView(_ webView: WKWebView, didFailProvisionalNavigation navigation: WKNavigation!, withError error: Error) {
            finishWithError(error)
        }
        func webView(_ webView: WKWebView, didFail navigation: WKNavigation!, withError error: Error) {
            finishWithError(error)
        }
        private func finishWithError(_ error: Error) {
            guard (error as NSError).code != NSURLErrorCancelled else { return }
            DispatchQueue.main.async { self.parent.isLoading = false; self.parent.failed = true }
        }

        // OAuth and its callback remain in the persistent app web view, keeping the
        // same server session throughout cTrader authorization.
        func webView(_ webView: WKWebView, createWebViewWith configuration: WKWebViewConfiguration,
                     for action: WKNavigationAction, windowFeatures: WKWindowFeatures) -> WKWebView? {
            guard action.targetFrame == nil, let target = action.request.url else { return nil }
            webView.load(URLRequest(url: target))
            return nil
        }
    }
}
