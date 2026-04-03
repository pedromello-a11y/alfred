"""Script temporário — adiciona colunas v3 na tabela tasks."""
import asyncio
import os
import sys


async def main():
    import asyncpg

    url = os.environ.get("DATABASE_URL", "")
    if not url:
        print("ERROR: DATABASE_URL não definida")
        sys.exit(1)

    # asyncpg precisa de postgresql:// puro (sem +asyncpg)
    url = url.replace("postgresql+asyncpg://", "postgresql://", 1)
    url = url.replace("postgres://", "postgresql://", 1)

    conn = await asyncpg.connect(url)

    statements = [
        "ALTER TABLE tasks ADD COLUMN IF NOT EXISTS checklist_json JSONB DEFAULT '[]'::jsonb",
        "ALTER TABLE tasks ADD COLUMN IF NOT EXISTS notes_json JSONB DEFAULT '[]'::jsonb",
        "ALTER TABLE tasks ADD COLUMN IF NOT EXISTS deadline_type VARCHAR(10) DEFAULT 'soft'",
    ]

    for sql in statements:
        print(f"Executando: {sql}")
        await conn.execute(sql)
        print("  ✓ OK")

    await conn.close()
    print("\n✅ Pronto. 3 colunas adicionadas (ou já existiam).")


asyncio.run(main())
