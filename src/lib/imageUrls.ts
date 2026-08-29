type ArtworkImage = {
  localImageUrl?: string | null;
  primaryImageSmall?: string | null;
  primaryImage?: string | null;
};

export const getGraphImageUrl = (artwork: ArtworkImage) => {
  if (artwork.localImageUrl) {
    return artwork.localImageUrl.replace(/\/full\.webp$/, '/graph.webp');
  }
  return artwork.primaryImageSmall || artwork.primaryImage || null;
};

export const getFullImageUrl = (artwork: ArtworkImage) =>
  artwork.localImageUrl || artwork.primaryImage || artwork.primaryImageSmall || null;

export const getImageSource = (artwork: ArtworkImage) => {
  if (artwork.localImageUrl) return 's3';
  if (artwork.primaryImageSmall) return 'met_small';
  if (artwork.primaryImage) return 'met_original';
  return null;
};
