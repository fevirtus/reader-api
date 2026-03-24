const { Client } = require('pg');
require('dotenv').config({ path: '.env.local' });

async function checkAndCreateDatabase() {
    const dbUrl = process.env.DATABASE_URL;
    if (!dbUrl) {
        console.error('DATABASE_URL is not set in .env.local');
        process.exit(1);
    }

    // Phân tích DATABASE_URL
    const match = dbUrl.match(/postgresql:\/\/([^:]+):([^@]+)@([^:]+):(\d+)\/([^?]+)/);
    if (!match) {
        console.error('Invalid DATABASE_URL format');
        process.exit(1);
    }

    const [_, user, password, host, port, database] = match;

    // Kết nối đến database mặc định 'postgres'
    const client = new Client({
        user,
        password: decodeURIComponent(password),
        host,
        port: parseInt(port),
        database: 'postgres',
    });

    try {
        await client.connect();

        const res = await client.query(`SELECT 1 FROM pg_database WHERE datname = $1`, [database]);

        if (res.rowCount === 0) {
            console.log(`Bắt đầu tạo database "${database}"...`);
            await client.query(`CREATE DATABASE "${database}"`);
            console.log(`Đã tạo thành công database "${database}".`);
        } else {
            console.log(`Database "${database}" đã tồn tại, bỏ qua tạo mới.`);
        }
    } catch (error) {
        console.error('Lỗi khi kiểm tra/tạo database:', error);
        process.exit(1);
    } finally {
        await client.end();
    }
}

checkAndCreateDatabase();
