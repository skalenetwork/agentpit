export const avatars = Object.values(
  import.meta.glob<ImageMetadata>("../assets/avatars/*.avif", { eager: true, import: "default" }),
);
