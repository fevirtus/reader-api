import { NextAuthOptions } from "next-auth"
import GoogleProvider from "next-auth/providers/google"
import { PrismaAdapter } from "@auth/prisma-adapter"
import { prisma } from "./prisma"

export const authOptions: NextAuthOptions = {
    adapter: PrismaAdapter(prisma) as any, // ép kiểu vì type mismatch nhỏ
    providers: [
        GoogleProvider({
            clientId: process.env.GOOGLE_CLIENT_ID || "demo-id",
            clientSecret: process.env.GOOGLE_CLIENT_SECRET || "demo-secret",
        }),
    ],
    session: {
        // Để giữ NextAuth dùng JWT thay vì lưu phiên vào DB nếu thích, nhưng khi dùng PrismaAdapter, mặc định nó dùng DB strategy.
        // strategy: "jwt",
    },
    callbacks: {
        async session({ session, user }) {
            if (session.user) {
                // Lấy role từ DB gán vào session
                const dbUser = await prisma.user.findUnique({
                    where: { email: session.user.email as string },
                    select: { role: true, id: true },
                })

                session.user.id = dbUser?.id || user.id
                session.user.role = dbUser?.role || "USER"
            }
            return session
        },
    },
    // Tuân thủ bảo mật NextAuth
    secret: process.env.NEXTAUTH_SECRET,
}
