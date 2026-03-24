import { NextResponse } from "next/server"
import { getServerSession } from "next-auth/next"
import { authOptions } from "@/lib/auth"
import { prisma } from "@/lib/prisma"

export async function GET(
    req: Request,
    context: { params: Promise<{ id: string }> }
) {
    const { id } = await context.params
    const session = await getServerSession(authOptions)
    if (!session || (session.user.role !== "MOD" && session.user.role !== "ADMIN")) {
        return NextResponse.json({ error: "Unauthorized" }, { status: 401 })
    }

    try {
        const novel = await prisma.novel.findFirst({
            where: session.user.role === "ADMIN"
                ? { id }
                : {
                    id,
                    OR: [
                        { uploaderId: session.user.id },
                        { uploaderId: null },
                    ],
                },
            include: {
                series: true,
                genres: {
                    include: {
                        genre: true
                    }
                }
            }
        })

        if (!novel) {
            return NextResponse.json({ error: "Novel not found" }, { status: 404 })
        }

        return NextResponse.json(novel)
    } catch (error) {
        return NextResponse.json({ error: "Failed to fetch novel details" }, { status: 500 })
    }
}
