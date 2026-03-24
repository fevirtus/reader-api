import { NextResponse } from "next/server"
import { getServerSession } from "next-auth/next"
import { authOptions } from "@/lib/auth"
import { prisma } from "@/lib/prisma"

export async function GET() {
    if (process.env.NODE_ENV === "production") {
        return NextResponse.json({ error: "Not found" }, { status: 404 })
    }

    const session = await getServerSession(authOptions)

    if (!session || !session.user || !session.user.email) {
        return NextResponse.json({ error: "Bạn phải đăng nhập trước" }, { status: 401 })
    }

    try {
        const updatedUser = await prisma.user.update({
            where: { email: session.user.email },
            data: { role: "MOD" },
        })

        return NextResponse.json({
            message: `Tài khoản ${updatedUser.email} đã được cấp quyền MOD. Xin hãy Đăng xuất và Đăng nhập lại để cập nhật phiên.`,
            user: updatedUser
        })
    } catch (error) {
        return NextResponse.json({ error: "Lỗi hệ thống" }, { status: 500 })
    }
}
