export const IMAGE_ASSET_KEY_VERSION = "v1";
export const IMAGE_ASSET_CACHE_CONTROL =
  "public, max-age=31536000, immutable";

export function getImageAssetKeys(normalizedPixelSha256: string) {
  const digest = normalizedPixelSha256.toLowerCase();

  if (!/^[a-f0-9]{64}$/.test(digest)) {
    throw new Error("normalizedPixelSha256 must be a 64-character hex digest");
  }

  const prefix = `assets/${IMAGE_ASSET_KEY_VERSION}/${digest.slice(0, 2)}/${digest}`;

  return {
    fullS3Key: `${prefix}/full.webp`,
    thumbnailS3Key: `${prefix}/graph.webp`,
  };
}
