require('dotenv').config();
const fs = require('fs');
const path = require('path');
const { Pool } = require('pg');

const pool = new Pool({
  connectionString: process.env.DATABASE_URL,
  ssl: process.env.DATABASE_URL && process.env.DATABASE_URL.includes('supabase')
    ? { rejectUnauthorized: false }
    : false,
});

const TABLE_NAME = process.env.WA_SESSION_TABLE || 'whatsapp_sessions';

class PostgresSessionStore {
  async sessionExists({ session }) {
    try {
      const { rows } = await pool.query(
        `SELECT id FROM ${TABLE_NAME} WHERE id = $1`,
        [session],
      );
      return rows.length > 0;
    } catch (err) {
      console.error('[session] sessionExists error:', err.message);
      return false;
    }
  }

  async save({ session }) {
    const zipPath = `${session}.zip`;
    if (!fs.existsSync(zipPath)) {
      console.log(`[session] ZIP não encontrado em ${zipPath}, pulando save`);
      return;
    }

    const base64 = fs.readFileSync(zipPath).toString('base64');
    const sessionKey = path.basename(session);

    try {
      await pool.query(
        `INSERT INTO ${TABLE_NAME} (id, data, updated_at)
         VALUES ($1, $2, NOW())
         ON CONFLICT (id) DO UPDATE SET data = $2, updated_at = NOW()`,
        [sessionKey, base64],
      );
      console.log('[session] ✅ Sessão salva no banco');
    } catch (err) {
      console.error('[session] Erro ao salvar sessão:', err.message);
    }
  }

  async extract({ session, path: zipPath }) {
    try {
      const { rows } = await pool.query(
        `SELECT data FROM ${TABLE_NAME} WHERE id = $1`,
        [session],
      );
      if (!rows[0]) {
        console.log('[session] Nenhuma sessão encontrada');
        return;
      }
      fs.mkdirSync(path.dirname(zipPath), { recursive: true });
      fs.writeFileSync(zipPath, Buffer.from(rows[0].data, 'base64'));
      console.log(`[session] ✅ Sessão restaurada em ${zipPath}`);
    } catch (err) {
      console.error('[session] Erro ao restaurar sessão:', err.message);
    }
  }

  async delete({ session }) {
    await pool.query(`DELETE FROM ${TABLE_NAME} WHERE id = $1`, [session]);
    console.log('[session] Sessão deletada');
  }
}

module.exports = PostgresSessionStore;
