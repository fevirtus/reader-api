"use client"

import { SessionProvider, useSession, signIn, signOut } from "next-auth/react"
import { useMemo, type ReactNode } from "react"
import type { User } from "./types"

export function AuthProvider({ children }: { children: ReactNode }) {
  return <SessionProvider>{children}</SessionProvider>
}

// Giữ nguyên custom hook `useAuth` để tương thích ngược với UI components hiện tại
export function useAuth() {
  const { data: session, status } = useSession()

  const isLoading = status === "loading"

  // Chuyển đổi session user thành format User của project
  const sessionUser = session?.user

  const user: User | null = useMemo(() => {
    if (!sessionUser) return null
    return {
      id: (sessionUser as any).id || "",
      username: (sessionUser as any).name || "Người dùng",
      email: (sessionUser as any).email || "",
      avatarUrl: (sessionUser as any).image || "",
      avatarColor: "bg-blue-500", // Mặc định
      role: (sessionUser as any).role || "USER",
      createdAt: new Date().toISOString().split("T")[0],
    }
  }, [sessionUser])

  const loginWithGoogle = () => signIn("google", { callbackUrl: "/" })
  const logout = () => signOut({ callbackUrl: "/" })

  return { user, isLoading, loginWithGoogle, logout }
}

