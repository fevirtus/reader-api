import mongoose from "mongoose"

let cached = (global as any).mongoose

if (!cached) {
    cached = (global as any).mongoose = { conn: null, promise: null }
}

async function connectToMongoDB() {
    const mongodbUri = process.env.MONGODB_URI
    if (!mongodbUri) {
        throw new Error("Please define the MONGODB_URI environment variable")
    }

    if (cached.conn) {
        return cached.conn
    }

    if (!cached.promise) {
        const opts = {
            bufferCommands: false,
        }

        cached.promise = mongoose.connect(mongodbUri, opts).then((mongoose) => {
            return mongoose
        })
    }

    try {
        cached.conn = await cached.promise
    } catch (e) {
        cached.promise = null
        throw e
    }

    return cached.conn
}

export default connectToMongoDB
