export interface Genre {
  id: string
  name: string
  slug: string
  description: string
  icon: string
}

export interface Novel {
  id: string
  title: string
  slug: string
  authorName: string
  series?: {
    id: string
    name: string
    slug: string
  } | null
  coverColor: string
  description: string
  genres: string[]
  status: "Đang ra" | "Hoàn thành" | "Tạm ngưng"
  totalChapters: number
  views: number
  rating: number
  ratingCount: number
  bookmarkCount: number
  lastUpdated: string
  createdAt: string
}

export interface Chapter {
  id: string
  novelId: string
  number: number
  volumeNumber?: number
  volumeTitle?: string
  volumeChapterNumber?: number
  title: string
  content: string
  views: number
  createdAt: string
}

export interface User {
  id: string
  username: string
  email: string
  avatarColor: string
  avatarUrl?: string
  role?: "USER" | "MOD" | "ADMIN"
  createdAt: string
}

export interface Comment {
  id: string
  userId: string
  username: string
  avatarColor: string
  novelId: string
  chapterId?: string
  content: string
  createdAt: string
}

export interface Bookmark {
  novelId: string
  lastChapterId?: string
  lastChapterNumber?: number
  addedAt: string
  novel?: any
}
