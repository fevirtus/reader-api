"use client"

import { createContext, useContext, useState, useEffect, useCallback, type ReactNode } from "react"
import type { Bookmark } from "./types"
import { useAuth } from "./auth-context"

interface BookmarkContextType {
  bookmarks: Bookmark[]
  isBookmarked: (novelId: string) => boolean
  toggleBookmark: (novelId: string) => Promise<void>
  updateProgress: (novelId: string, chapterId: string, chapterNumber: number) => Promise<void>
  getProgress: (novelId: string) => Bookmark | undefined
}

const BookmarkContext = createContext<BookmarkContextType | undefined>(undefined)

export function BookmarkProvider({ children }: { children: ReactNode }) {
  const { user } = useAuth()
  const [bookmarks, setBookmarks] = useState<Bookmark[]>([])

  const fetchBookmarks = useCallback(async () => {
    if (!user) {
      setBookmarks([])
      return
    }

    try {
      const res = await fetch("/api/user/bookmarks")
      if (!res.ok) return

      const data = await res.json()
      setBookmarks(Array.isArray(data) ? data : [])
    } catch (e) {
      console.error("Failed to fetch bookmarks", e)
    }
  }, [user])

  useEffect(() => {
    fetchBookmarks()
  }, [fetchBookmarks])

  const toggleBookmark = useCallback(async (novelId: string) => {
    if (!user) return

    // Optimistic update
    setBookmarks((prev) => {
      const exists = prev.find((b) => b.novelId === novelId)
      if (exists) {
        return prev.filter((b) => b.novelId !== novelId)
      }
      return [...prev, { novelId, addedAt: new Date().toISOString() } as any]
    })

    try {
      const res = await fetch("/api/user/bookmarks", {
        method: "POST",
        headers: { "Content-Type": "application/json" },
        body: JSON.stringify({ action: "toggle", novelId })
      })

      if (!res.ok) {
        throw new Error("Không thể cập nhật đánh dấu")
      }

      await fetchBookmarks()
    } catch (e) {
      console.error(e)
      await fetchBookmarks()
    }
  }, [fetchBookmarks, user])

  const updateProgress = useCallback(async (novelId: string, chapterId: string, chapterNumber: number) => {
    if (!user) return

    // Optimistic update
    setBookmarks((prev) => {
      const exists = prev.find((b) => b.novelId === novelId)
      if (exists) {
        return prev.map(b => b.novelId === novelId ? { ...b, lastChapterId: chapterId, lastChapterNumber: chapterNumber } : b)
      }
      return [...prev, { novelId, lastChapterId: chapterId, lastChapterNumber: chapterNumber, addedAt: new Date().toISOString() } as any]
    })

    try {
      const res = await fetch("/api/user/bookmarks", {
        method: "POST",
        headers: { "Content-Type": "application/json" },
        body: JSON.stringify({ action: "updateProgress", novelId, lastChapterId: chapterId, lastChapterNumber: chapterNumber })
      })

      if (!res.ok) {
        throw new Error("Không thể cập nhật tiến độ")
      }

      await fetchBookmarks()
    } catch (e) {
      console.error(e)
      await fetchBookmarks()
    }
  }, [fetchBookmarks, user])

  const getProgress = useCallback((novelId: string) => {
    return bookmarks.find((b) => b.novelId === novelId)
  }, [bookmarks])

  const isBookmarked = useCallback((novelId: string) => {
    return bookmarks.some((b) => b.novelId === novelId)
  }, [bookmarks])

  return (
    <BookmarkContext.Provider value={{ bookmarks, isBookmarked, toggleBookmark, updateProgress, getProgress }}>
      {children}
    </BookmarkContext.Provider>
  )
}

export function useBookmarks() {
  const context = useContext(BookmarkContext)
  if (!context) throw new Error("useBookmarks must be used within BookmarkProvider")
  return context
}
