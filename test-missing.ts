import { PrismaClient } from '@prisma/client'
const prisma = new PrismaClient()

async function main() {
  const isMissingFilter = true
  const baseWhereOptions: any[] = [{}]

  if (isMissingFilter) {
    baseWhereOptions.push({
      OR: [
        { authorName: { in: ["", "Chưa rõ"] } },
        { description: "" },
        { description: { in: ["Chưa có giới thiệu", "Không có giới thiệu", "chưa có giới thiệu", "không có", "chưa rõ", "đang cập nhật"] } }
      ],
    })
  }

  const rows = await prisma.novel.findMany({
    where: {
      AND: baseWhereOptions,
    },
    select: { id: true },
    orderBy: [{ updatedAt: "desc" }],
    take: 25,
    skip: 0,
  })

  console.log("Returned rows length:", rows.length)
}
main().finally(() => prisma.$disconnect())
