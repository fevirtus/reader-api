export function getNovelStatusBadgeClass(status: string): string {
  const normalized = status.trim().toLowerCase()

  if (normalized.includes("hoàn")) {
    return "bg-emerald-100 text-emerald-700 dark:bg-emerald-900/30 dark:text-emerald-300"
  }

  if (normalized.includes("tạm")) {
    return "bg-amber-100 text-amber-700 dark:bg-amber-900/30 dark:text-amber-300"
  }

  if (normalized.includes("drop") || normalized.includes("hủy") || normalized.includes("cancel")) {
    return "bg-rose-100 text-rose-700 dark:bg-rose-900/30 dark:text-rose-300"
  }

  return "bg-sky-100 text-sky-700 dark:bg-sky-900/30 dark:text-sky-300"
}
