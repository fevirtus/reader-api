export const MOD_AI_PREFILL_STORAGE_KEY = "mod:ai-tool:novel-prefill"
export const MOD_AI_MODEL_STORAGE_KEY = "mod:ai-tool:model"
export const MOD_AI_WEB_DEFAULT_MODEL = "gpt-4o-mini-search-preview"

export const MOD_AI_WEB_MODEL_OPTIONS = [
  {
    value: "gpt-4o-mini-search-preview",
    label: "gpt-4o-mini-search-preview (nhanh)",
  },
  {
    value: "gpt-4o-search-preview",
    label: "gpt-4o-search-preview (chat luong cao)",
  },
] as const

export type AINovelPrefillPayload = {
  title?: string
  originalTitle?: string
  authorName?: string
  originalAuthorName?: string
  description?: string
  coverUrl?: string
  status?: "Đang ra" | "Hoàn thành" | "Tạm ngưng"
  genresSuggested?: string[]
}
