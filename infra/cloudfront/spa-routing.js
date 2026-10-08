// SortView React app: SPA routing, as a CloudFront Function (runtime cloudfront-js-2.0, event type viewer-request).
//
// Attach to the distribution's DEFAULT (S3) behavior ONLY -- never to /api/*. See docs/react-hosting-runbook.md.
//
// The app uses BrowserRouter, so an address like /organizations/acme/sorters/main/reports is a route the
// browser understands, not a file in the bucket. Every request on this behavior is served /index.html except:
//
//   /           served by the distribution's default root object (index.html)
//   /assets/*   the build's hashed JS and CSS: served as stored, so a missing file is a real S3 error
//   /api/*      never reaches this function (it has its own behavior); left alone in case it ever does
//
// The decision is by path prefix, not by "does it look like a file": a route segment (a slug) may contain a dot.
// Nothing else is touched: no redirect, no query string, no cookie, no header.

function handler(event) {
    var request = event.request;
    var uri = request.uri;

    if (uri === '/' || uri.startsWith('/assets/') || uri.startsWith('/api/')) {
        return request;
    }

    request.uri = '/index.html';
    return request;
}
