# Image Storage Architecture

## Delivery

- Bucket: `met-artworks-images` in `us-east-1`.
- CloudFront distribution: `E3B3NJO0AQL0KT`.
- Delivery base URL: `https://d2pvxr3eb77vb4.cloudfront.net`.
- Origin access control: `E1PGMOH6LC5HOA`, using SigV4 with signing always enabled.
- The S3 REST origin is private. Only the CloudFront distribution may read objects.
- CloudFront redirects HTTP to HTTPS, supports HTTP/2 and HTTP/3, and permits only `GET` and `HEAD`.
- The account does not own a Route 53 zone or ACM certificate for `openmetropolitan.com`, so the stable CloudFront hostname is the approved final delivery domain.

Database image assets store object keys, not delivery URLs. The API or client constructs a delivery URL by joining `IMAGE_CDN_BASE_URL` and the stored key.

## Object Keys

Canonical objects use the normalized-pixel SHA-256 digest and a versioned encoding namespace:

```text
assets/v1/{first-two-digest-characters}/{normalized-pixel-sha256}/full.webp
assets/v1/{first-two-digest-characters}/{normalized-pixel-sha256}/graph.webp
```

The `v1` namespace represents full-size WebP quality 82 and a 512 px graph derivative at WebP quality 85, both encoded with method 4. If those settings later change incompatibly, increment the namespace instead of overwriting immutable objects.

Legacy objects remain at `artworks/{artworkId}.jpg` through the existing-image migration and rollback window.

## Caching and CORS

- New canonical objects use `Content-Type: image/webp`.
- New canonical objects use `Cache-Control: public, max-age=31536000, immutable`.
- CloudFront uses the AWS managed `CachingOptimized` cache policy.
- CloudFront uses the AWS managed `SimpleCORS` response-headers policy, which returns `Access-Control-Allow-Origin: *` for image reads.
- S3 has no browser CORS policy because browsers never access the private origin directly.

## Retention and Rollback

- S3 versioning is enabled.
- Noncurrent versions are retained for 30 days.
- Incomplete multipart uploads are aborted after 7 days.
- Active canonical assets remain in S3 Standard.
- Legacy `artworks/*.jpg` objects are not deleted or transitioned during benchmarking or migration.
- After Priority 3 is verified, legacy and redundant objects remain available through CloudFront for the 30-day rollback window. Archive them only after database links, API delivery, and rollback have been verified.
- Rollback keeps the CloudFront hostname and restores artwork associations or legacy object keys; it does not require making S3 public again.

## Cost Guardrail

- AWS Budget: `met-galaxy-monthly`.
- Monthly limit: `$60`.
- Actual-cost alerts: `50%`, `80%`, and `100%`.
- Forecasted-cost alert: `100%`.
- Revisit the limit after the Priority 1 benchmark replaces the preliminary ingestion estimate.

## Infrastructure Files

- `cloudfront-distribution.json` records the deployed distribution configuration.
- `s3-bucket-policy.json` grants read access only to the deployed CloudFront distribution.
- `s3-public-access-block.json` blocks all public bucket and ACL access.
- `s3-lifecycle.json` defines the rollback and incomplete-upload retention rules.
