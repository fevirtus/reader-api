const { PrismaClient } = require('@prisma/client')
const mongoose = require('mongoose')
require('dotenv').config({ path: '.env.local' })
require('dotenv').config()

const prisma = new PrismaClient()

async function main() {
    console.log('Connecting to MongoDB...')
    // Connect to MongoDB using MONGODB_URI
    const mongoUri = process.env.MONGODB_URI
    if (!mongoUri) {
        throw new Error('MONGODB_URI is not defined in env')
    }
    await mongoose.connect(mongoUri)

    // Wipe MongoDB Chapters
    console.log('Wiping chapters from MongoDB...')
    try {
        const chapterSchema = new mongoose.Schema({}, { strict: false })
        const Chapter = mongoose.models.Chapter || mongoose.model('Chapter', chapterSchema, 'chapters')
        const res = await Chapter.deleteMany({})
        console.log(`Deleted ${res.deletedCount} chapters.`)
    } catch (e) {
        console.error('Error wiping mongo chapters', e)
    }

    // Wipe PostgreSQL Content
    console.log('Wiping Novels, Genres, Comments, Bookmarks from PostgreSQL...')
    try {
        // Delete in order to respect foreign keys if Cascade isn't perfect, but Cascade is set on most.
        await prisma.comment.deleteMany({})
        console.log('Deleted all comments.')

        await prisma.bookmark.deleteMany({})
        console.log('Deleted all bookmarks.')

        await prisma.novelGenre.deleteMany({})
        console.log('Deleted all novel_genres.')

        await prisma.genre.deleteMany({})
        console.log('Deleted all genres.')

        await prisma.novel.deleteMany({})
        console.log('Deleted all novels.')

    } catch (error) {
        console.error('Error wiping postgres', error)
    }

    console.log('Cleanup complete.')
}

main()
    .catch(console.error)
    .finally(async () => {
        await prisma.$disconnect()
        await mongoose.disconnect()
        process.exit(0)
    })
