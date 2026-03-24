"use client"

import { createContext, ReactNode, useCallback, useContext, useEffect, useState } from "react"
import { useAuth } from "@/lib/auth-context"

type UserRecommendedNovel = {
  id: string
  title: string
  slug: string
  authorName: string
  coverUrl: string | null
  status: string
  totalChapters: number
}

type UserRecommendationItem = {
  id: string
  novelId: string
  createdAt: string | null
  novel: UserRecommendedNovel
}

type RecommendationContextType = {
  recommendations: UserRecommendationItem[]
  isRecommended: (novelId: string) => boolean
  toggleRecommendation: (novelId: string) => Promise<{ status: "added" | "removed" | "exists" }>
}

const RecommendationContext = createContext<RecommendationContextType | undefined>(undefined)

export function RecommendationProvider({ children }: { children: ReactNode }) {
  const { user } = useAuth()
  const [recommendations, setRecommendations] = useState<UserRecommendationItem[]>([])

  const fetchRecommendations = useCallback(async () => {
    if (!user) {
      setRecommendations([])
      return
    }

    try {
      const res = await fetch("/api/user/recommendations")
      if (!res.ok) {
        setRecommendations([])
        return
      }

      const data = await res.json()
      setRecommendations(Array.isArray(data) ? data : [])
    } catch (error) {
      console.error("Failed to fetch recommendations", error)
    }
  }, [user])

  useEffect(() => {
    fetchRecommendations()
  }, [fetchRecommendations])

  const isRecommended = useCallback(
    (novelId: string) => recommendations.some((item) => item.novelId === novelId),
    [recommendations]
  )

  const toggleRecommendation = useCallback(
    async (novelId: string) => {
      if (!user) throw new Error("Unauthorized")
      if (!novelId) throw new Error("Missing novel id")

      const existed = recommendations.some((item) => item.novelId === novelId)

      if (existed) {
        const res = await fetch(`/api/user/recommendations?novelId=${encodeURIComponent(novelId)}`, {
          method: "DELETE",
        })
        const data = (await res.json()) as { error?: string }

        if (!res.ok) {
          throw new Error(data.error || "Không thể bỏ đề cử")
        }

        await fetchRecommendations()
        return { status: "removed" as const }
      }

      const res = await fetch("/api/user/recommendations", {
        method: "POST",
        headers: { "Content-Type": "application/json" },
        body: JSON.stringify({ novelId }),
      })

      const data = (await res.json()) as { error?: string }

      if (!res.ok) {
        if (res.status === 409) {
          await fetchRecommendations()
          return { status: "exists" as const }
        }

        throw new Error(data.error || "Không thể đề cử truyện")
      }

      await fetchRecommendations()
      return { status: "added" as const }
    },
    [fetchRecommendations, recommendations, user]
  )

  return (
    <RecommendationContext.Provider
      value={{
        recommendations,
        isRecommended,
        toggleRecommendation,
      }}
    >
      {children}
    </RecommendationContext.Provider>
  )
}

export function useRecommendations() {
  const context = useContext(RecommendationContext)
  if (!context) throw new Error("useRecommendations must be used within RecommendationProvider")
  return context
}
