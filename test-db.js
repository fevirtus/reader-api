const { MongoClient } = require('mongodb');

async function check() {
    const uri = process.env.MONGODB_URI || "mongodb://localhost:27017/reader";
    const client = new MongoClient(uri);
    try {
        await client.connect();
        const db = client.db();
        // Just find the latest 5 chapters inserted
        const docs = await db.collection('Chapter').find({}).sort({ _id: -1 }).limit(10).toArray();
        console.log("Recent chapters:");
        docs.forEach(d => console.log(d.novelId, d.number, d.title));
    } finally {
        await client.close();
    }
}
check();
