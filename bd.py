"""Camada de banco do Atlas.

Suporta dois modos:

1. PostgreSQL persistente em produção, quando DATABASE_URL está configurado.
2. SQLite local para desenvolvimento, quando DATABASE_URL não existe.

No Render, use PostgreSQL/Neon/Supabase e configure DATABASE_URL nas variáveis de
ambiente. SQLite local dentro do web service pode perder dados em restart/redeploy.
"""
from __future__ import annotations

import os
import re
import sqlite3
from datetime import datetime
from pathlib import Path
from typing import Any, Iterable, Optional

BASE_DIR = Path(__file__).resolve().parent
DB_PATH = Path(os.getenv("DB_PATH", BASE_DIR / "atlas.sqlite3"))
DB_PATH.parent.mkdir(parents=True, exist_ok=True)

DATABASE_URL = os.getenv("DATABASE_URL", "").strip()
USAR_POSTGRES = bool(DATABASE_URL)

TABELAS_COM_ID = {
    "professores", "alunos", "avaliacoes", "provas", "folhas_resposta",
    "resultados", "questoes_cache", "tarefas_ia", "sessoes_scan",
    "leituras_scan", "anotacoes", "diario_bordo", "checklist_tcc",
    "cronograma_tarefas", "questoes_anuladas", "listas_exercicios",
    "identificacoes_pendentes", "arquivos_ano_letivo",
}


class Row(dict):
    """Linha compatível com sqlite3.Row.

    Permite acesso por nome: row["nome"] e, quando necessário, por índice: row[0].
    """

    def __init__(self, keys: list[str], values: Iterable[Any]):
        super().__init__(zip(keys, values))
        self._keys_order = list(keys)

    def __getitem__(self, key: Any) -> Any:
        if isinstance(key, int):
            return dict.__getitem__(self, self._keys_order[key])
        return dict.__getitem__(self, key)

    def keys(self):  # mantém o comportamento usado nos templates/rotas
        return dict.keys(self)


class PgResult:
    def __init__(self, rows: Optional[list[Row]] = None, rowcount: int = -1, lastrowid: Optional[int] = None):
        self._rows = rows or []
        self.rowcount = rowcount
        self.lastrowid = lastrowid

    def fetchone(self):
        return self._rows[0] if self._rows else None

    def fetchall(self):
        return list(self._rows)


class PgConnection:
    """Adaptador pequeno para o app continuar usando conn.execute(sql, params).

    O projeto nasceu em SQLite. Este adaptador converte os pontos principais para
    PostgreSQL: placeholders, INSERT OR IGNORE, INSERT OR REPLACE, sqlite_master
    e RETURNING id para manter cursor.lastrowid funcionando.
    """

    def __init__(self):
        import psycopg2
        url = DATABASE_URL.replace("postgres://", "postgresql://", 1)
        self._conn = psycopg2.connect(url)
        self._conn.autocommit = False

    def execute(self, sql: str, params: Optional[Iterable[Any]] = None) -> PgResult:
        sql_traduzida, params_traduzidos = self._traduzir_sql(sql, params)
        cur = self._conn.cursor()
        try:
            cur.execute(sql_traduzida, params_traduzidos)
            rows: list[Row] = []
            lastrowid = None
            if cur.description:
                keys = [desc[0] for desc in cur.description]
                dados = cur.fetchall()
                rows = [Row(keys, linha) for linha in dados]
                if rows and "id" in rows[0].keys():
                    try:
                        lastrowid = int(rows[0]["id"])
                    except Exception:
                        lastrowid = None
            return PgResult(rows=rows, rowcount=cur.rowcount, lastrowid=lastrowid)
        finally:
            cur.close()

    def commit(self):
        self._conn.commit()

    def rollback(self):
        self._conn.rollback()

    def close(self):
        self._conn.close()

    def _traduzir_sql(self, sql: str, params: Optional[Iterable[Any]]):
        original = " ".join(sql.strip().split())
        params_tuple = tuple(params or ())

        if not original:
            return original, params_tuple

        # Transações SQLite.
        if original.upper() == "BEGIN IMMEDIATE":
            return "BEGIN", params_tuple

        # PRAGMAs não existem no PostgreSQL.
        if original.upper().startswith("PRAGMA"):
            return "SELECT 1", params_tuple

        # Consultas equivalentes ao sqlite_master.
        low = original.lower()
        if "from sqlite_master" in low and "count" in low:
            return (
                "SELECT COUNT(*) AS total FROM information_schema.tables "
                "WHERE table_schema = 'public' AND table_type = 'BASE TABLE'",
                (),
            )
        if "from sqlite_master" in low and "order by name" in low:
            return (
                "SELECT table_name AS name FROM information_schema.tables "
                "WHERE table_schema = 'public' AND table_type = 'BASE TABLE' "
                "ORDER BY table_name",
                (),
            )
        if "from sqlite_master" in low and "name=?" in low:
            return (
                "SELECT table_name AS name FROM information_schema.tables "
                "WHERE table_schema = 'public' AND table_type = 'BASE TABLE' AND table_name=%s",
                params_tuple,
            )

        sql2 = sql.strip()
        sql2 = sql2.replace("datetime('now')", "CURRENT_TIMESTAMP")
        sql2 = sql2.replace("?", "%s")

        # INSERT OR REPLACE: usado no projeto para folhas_resposta.
        replace_match = re.search(r"INSERT\s+OR\s+REPLACE\s+INTO\s+(\w+)\s*\((.*?)\)\s*VALUES", sql2, flags=re.I | re.S)
        if replace_match:
            tabela = replace_match.group(1)
            colunas = [c.strip() for c in replace_match.group(2).replace("\n", " ").split(",")]
            sql2 = re.sub(r"INSERT\s+OR\s+REPLACE\s+INTO", "INSERT INTO", sql2, flags=re.I)
            alvo = "prova_id" if tabela == "folhas_resposta" else "id"
            atualizaveis = [c for c in colunas if c not in {"id", alvo}]
            if atualizaveis:
                sets = ", ".join(f"{c}=EXCLUDED.{c}" for c in atualizaveis)
                sql2 = f"{sql2} ON CONFLICT ({alvo}) DO UPDATE SET {sets}"
            else:
                sql2 = f"{sql2} ON CONFLICT ({alvo}) DO NOTHING"

        # INSERT OR IGNORE.
        insert_ignore = bool(re.search(r"INSERT\s+OR\s+IGNORE\s+INTO", sql2, flags=re.I))
        if insert_ignore:
            sql2 = re.sub(r"INSERT\s+OR\s+IGNORE\s+INTO", "INSERT INTO", sql2, flags=re.I)
            if "ON CONFLICT" not in sql2.upper():
                sql2 = f"{sql2} ON CONFLICT DO NOTHING"

        # Para manter cursor.lastrowid nos INSERTs.
        if self._deve_retornar_id(sql2):
            sql2 = f"{sql2} RETURNING id"

        return sql2, params_tuple

    def _deve_retornar_id(self, sql: str) -> bool:
        texto = " ".join(sql.strip().split())
        if not texto.upper().startswith("INSERT INTO"):
            return False
        if " RETURNING " in texto.upper():
            return False
        match = re.match(r"INSERT\s+INTO\s+(\w+)", texto, flags=re.I)
        if not match:
            return False
        return match.group(1) in TABELAS_COM_ID


def conectar():
    if USAR_POSTGRES:
        return PgConnection()

    conn = sqlite3.connect(DB_PATH, timeout=30, check_same_thread=False)
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA journal_mode=WAL")
    conn.execute("PRAGMA busy_timeout=30000")
    conn.execute("PRAGMA foreign_keys=ON")
    return conn


def _tabela_existe_sqlite(conn, tabela):
    row = conn.execute(
        "SELECT name FROM sqlite_master WHERE type='table' AND name=?",
        (tabela,),
    ).fetchone()
    return row is not None


def _tabela_existe_postgres(conn, tabela):
    row = conn.execute(
        """
        SELECT table_name AS name
        FROM information_schema.tables
        WHERE table_schema = 'public' AND table_type = 'BASE TABLE' AND table_name = ?
        """,
        (tabela,),
    ).fetchone()
    return row is not None


def _tabela_existe(conn, tabela):
    return _tabela_existe_postgres(conn, tabela) if USAR_POSTGRES else _tabela_existe_sqlite(conn, tabela)


def _colunas_tabela_sqlite(conn, tabela):
    if not _tabela_existe_sqlite(conn, tabela):
        return set()
    return {linha[1] for linha in conn.execute(f"PRAGMA table_info({tabela})").fetchall()}


def _colunas_tabela_postgres(conn, tabela):
    if not _tabela_existe_postgres(conn, tabela):
        return set()
    rows = conn.execute(
        """
        SELECT column_name
        FROM information_schema.columns
        WHERE table_schema = 'public' AND table_name = ?
        """,
        (tabela,),
    ).fetchall()
    return {r["column_name"] for r in rows}


def _colunas_tabela(conn, tabela):
    return _colunas_tabela_postgres(conn, tabela) if USAR_POSTGRES else _colunas_tabela_sqlite(conn, tabela)


def _adicionar_coluna_se_nao_existir(conn, tabela, coluna, definicao):
    if not _tabela_existe(conn, tabela):
        return
    if coluna not in _colunas_tabela(conn, tabela):
        conn.execute(f"ALTER TABLE {tabela} ADD COLUMN {coluna} {definicao}")


def _agora():
    return datetime.now().strftime("%Y-%m-%d %H:%M:%S")


def _garantir_professor_padrao(conn):
    row = conn.execute("SELECT id FROM professores ORDER BY id LIMIT 1").fetchone()
    if row:
        return int(row["id"])
    cursor = conn.execute(
        """
        INSERT INTO professores (nome, email, senha_hash, criado_em)
        VALUES (?, ?, ?, ?)
        """,
        ("Professor Demo", "demo@atlas.local", "", _agora()),
    )
    return int(cursor.lastrowid)


POSTGRES_SCHEMA = [
    """
    CREATE TABLE IF NOT EXISTS professores (
        id SERIAL PRIMARY KEY,
        nome TEXT NOT NULL,
        email TEXT NOT NULL UNIQUE,
        senha_hash TEXT NOT NULL,
        criado_em TEXT NOT NULL
    )
    """,
    """
    CREATE TABLE IF NOT EXISTS alunos (
        id SERIAL PRIMARY KEY,
        professor_id INTEGER REFERENCES professores(id),
        nome TEXT NOT NULL,
        matricula TEXT NOT NULL,
        turma TEXT NOT NULL,
        criado_em TEXT,
        ano_letivo TEXT,
        arquivado INTEGER DEFAULT 0,
        UNIQUE(professor_id, matricula)
    )
    """,
    """
    CREATE TABLE IF NOT EXISTS avaliacoes (
        id SERIAL PRIMARY KEY,
        professor_id INTEGER REFERENCES professores(id),
        titulo TEXT NOT NULL,
        materias TEXT NOT NULL,
        temas TEXT,
        total_questoes INTEGER NOT NULL,
        especificacoes TEXT,
        questoes_json TEXT NOT NULL,
        gabarito_json TEXT NOT NULL,
        status_revisao TEXT DEFAULT 'rascunho',
        criado_em TEXT NOT NULL,
        atualizado_em TEXT,
        ano_letivo TEXT,
        arquivado INTEGER DEFAULT 0
    )
    """,
    """
    CREATE TABLE IF NOT EXISTS provas (
        id SERIAL PRIMARY KEY,
        professor_id INTEGER REFERENCES professores(id),
        avaliacao_id INTEGER REFERENCES avaliacoes(id),
        titulo TEXT NOT NULL,
        disciplina TEXT NOT NULL,
        aluno_id INTEGER NOT NULL REFERENCES alunos(id),
        total_questoes INTEGER NOT NULL,
        gabarito_json TEXT NOT NULL,
        qr_arquivo TEXT,
        tipo_prova TEXT DEFAULT 'A',
        mapa_alternativas_json TEXT DEFAULT '{}',
        criado_em TEXT NOT NULL,
        ano_letivo TEXT,
        arquivado INTEGER DEFAULT 0
    )
    """,
    """
    CREATE TABLE IF NOT EXISTS folhas_resposta (
        id SERIAL PRIMARY KEY,
        professor_id INTEGER REFERENCES professores(id),
        avaliacao_id INTEGER REFERENCES avaliacoes(id),
        prova_id INTEGER NOT NULL UNIQUE REFERENCES provas(id),
        aluno_id INTEGER NOT NULL REFERENCES alunos(id),
        tipo_prova TEXT DEFAULT 'A',
        mapa_alternativas_json TEXT DEFAULT '{}',
        criado_em TEXT NOT NULL
    )
    """,
    """
    CREATE TABLE IF NOT EXISTS resultados (
        id SERIAL PRIMARY KEY,
        professor_id INTEGER REFERENCES professores(id),
        aluno_id INTEGER REFERENCES alunos(id),
        prova_id INTEGER NOT NULL REFERENCES provas(id),
        nota_percentual DOUBLE PRECISION NOT NULL,
        resultado_json TEXT NOT NULL,
        status_confianca TEXT DEFAULT 'confiavel',
        criado_em TEXT NOT NULL,
        ano_letivo TEXT,
        arquivado INTEGER DEFAULT 0
    )
    """,
    """
    CREATE TABLE IF NOT EXISTS questoes_cache (
        id SERIAL PRIMARY KEY,
        professor_id INTEGER REFERENCES professores(id),
        materia TEXT NOT NULL,
        tema TEXT,
        dificuldade TEXT DEFAULT 'medio',
        modelo TEXT DEFAULT 'ENEM',
        contexto TEXT,
        enunciado TEXT NOT NULL,
        alternativas_json TEXT NOT NULL,
        correta TEXT NOT NULL,
        habilidade TEXT,
        explicacao TEXT,
        origem TEXT DEFAULT 'ia',
        hash TEXT UNIQUE,
        aprovado INTEGER DEFAULT 1,
        usado_vezes INTEGER DEFAULT 0,
        criado_em TEXT NOT NULL,
        atualizado_em TEXT NOT NULL,
        ano_letivo TEXT,
        arquivado INTEGER DEFAULT 0
    )
    """,
    """
    CREATE TABLE IF NOT EXISTS tarefas_ia (
        id SERIAL PRIMARY KEY,
        tipo TEXT NOT NULL,
        status TEXT NOT NULL DEFAULT 'pendente',
        professor_id INTEGER REFERENCES professores(id),
        dados_json TEXT NOT NULL,
        resultado_id INTEGER,
        erro TEXT,
        progresso INTEGER DEFAULT 0,
        mensagem TEXT,
        criado_em TEXT NOT NULL,
        atualizado_em TEXT NOT NULL
    )
    """,
    """
    CREATE TABLE IF NOT EXISTS sessoes_scan (
        id SERIAL PRIMARY KEY,
        professor_id INTEGER REFERENCES professores(id),
        prova_base_id INTEGER,
        modo TEXT NOT NULL DEFAULT 'individual',
        status TEXT NOT NULL DEFAULT 'aberta',
        total_processados INTEGER DEFAULT 0,
        total_sucesso INTEGER DEFAULT 0,
        total_revisao INTEGER DEFAULT 0,
        tempo_medio_ms DOUBLE PRECISION DEFAULT 0,
        criado_em TEXT NOT NULL,
        finalizado_em TEXT,
        finalizada_em TEXT
    )
    """,
    """
    CREATE TABLE IF NOT EXISTS leituras_scan (
        id SERIAL PRIMARY KEY,
        sessao_id INTEGER NOT NULL REFERENCES sessoes_scan(id),
        aluno_id INTEGER REFERENCES alunos(id),
        prova_id INTEGER NOT NULL REFERENCES provas(id),
        resultado_id INTEGER REFERENCES resultados(id),
        nota_percentual DOUBLE PRECISION,
        latencia_ms DOUBLE PRECISION,
        status TEXT NOT NULL,
        mensagem TEXT,
        criado_em TEXT NOT NULL
    )
    """,
    """
    CREATE TABLE IF NOT EXISTS anotacoes (
        id SERIAL PRIMARY KEY,
        professor_id INTEGER NOT NULL REFERENCES professores(id),
        titulo TEXT NOT NULL,
        conteudo TEXT NOT NULL,
        categoria TEXT DEFAULT 'Geral',
        avaliacao_id INTEGER REFERENCES avaliacoes(id),
        turma TEXT,
        importante INTEGER DEFAULT 0,
        criado_em TEXT NOT NULL,
        atualizado_em TEXT,
        ano_letivo TEXT,
        arquivado INTEGER DEFAULT 0
    )
    """,
    """
    CREATE TABLE IF NOT EXISTS diario_bordo (
        id SERIAL PRIMARY KEY,
        professor_id INTEGER NOT NULL REFERENCES professores(id),
        data TEXT NOT NULL,
        atividade TEXT NOT NULL,
        responsavel TEXT,
        status TEXT DEFAULT 'feito',
        observacoes TEXT,
        criado_em TEXT NOT NULL,
        atualizado_em TEXT,
        ano_letivo TEXT,
        arquivado INTEGER DEFAULT 0
    )
    """,
    """
    CREATE TABLE IF NOT EXISTS checklist_tcc (
        id SERIAL PRIMARY KEY,
        professor_id INTEGER NOT NULL REFERENCES professores(id),
        titulo TEXT NOT NULL,
        categoria TEXT DEFAULT 'TCC',
        concluido INTEGER DEFAULT 0,
        ordem INTEGER DEFAULT 0,
        criado_em TEXT NOT NULL,
        atualizado_em TEXT,
        ano_letivo TEXT,
        arquivado INTEGER DEFAULT 0
    )
    """,
    """
    CREATE TABLE IF NOT EXISTS cronograma_tarefas (
        id SERIAL PRIMARY KEY,
        professor_id INTEGER NOT NULL REFERENCES professores(id),
        titulo TEXT NOT NULL,
        descricao TEXT,
        responsavel TEXT,
        status TEXT DEFAULT 'afazer',
        prioridade TEXT DEFAULT 'media',
        prazo TEXT,
        criado_em TEXT NOT NULL,
        atualizado_em TEXT,
        ano_letivo TEXT,
        arquivado INTEGER DEFAULT 0
    )
    """,
    """
    CREATE TABLE IF NOT EXISTS questoes_anuladas (
        id SERIAL PRIMARY KEY,
        professor_id INTEGER NOT NULL REFERENCES professores(id),
        avaliacao_id INTEGER NOT NULL REFERENCES avaliacoes(id),
        questao INTEGER NOT NULL,
        motivo TEXT,
        criado_em TEXT NOT NULL,
        UNIQUE(professor_id, avaliacao_id, questao)
    )
    """,
    """
    CREATE TABLE IF NOT EXISTS listas_exercicios (
        id SERIAL PRIMARY KEY,
        professor_id INTEGER NOT NULL REFERENCES professores(id),
        titulo TEXT NOT NULL,
        filtros_json TEXT DEFAULT '{}',
        questoes_json TEXT NOT NULL,
        incluir_gabarito INTEGER DEFAULT 0,
        arquivo_pdf TEXT,
        criado_em TEXT NOT NULL
    )
    """,
    """
    CREATE TABLE IF NOT EXISTS identificacoes_pendentes (
        id SERIAL PRIMARY KEY,
        professor_id INTEGER NOT NULL REFERENCES professores(id),
        avaliacao_id INTEGER REFERENCES avaliacoes(id),
        prova_base_id INTEGER REFERENCES provas(id),
        imagem_arquivo TEXT,
        texto_detectado TEXT,
        sugestoes_json TEXT DEFAULT '[]',
        status TEXT DEFAULT 'pendente',
        aluno_id_confirmado INTEGER REFERENCES alunos(id),
        criado_em TEXT NOT NULL,
        atualizado_em TEXT
    )
    """,
    """
    CREATE TABLE IF NOT EXISTS arquivos_ano_letivo (
        id SERIAL PRIMARY KEY,
        professor_id INTEGER NOT NULL REFERENCES professores(id),
        ano_letivo TEXT NOT NULL,
        descricao TEXT,
        totais_json TEXT DEFAULT '{}',
        criado_em TEXT NOT NULL
    )
    """,
]

INDICES = [
    "CREATE INDEX IF NOT EXISTS idx_alunos_prof_turma ON alunos (professor_id, turma, nome)",
    "CREATE INDEX IF NOT EXISTS idx_avaliacoes_prof ON avaliacoes (professor_id, id DESC)",
    "CREATE INDEX IF NOT EXISTS idx_provas_prof_avaliacao ON provas (professor_id, avaliacao_id, aluno_id)",
    "CREATE INDEX IF NOT EXISTS idx_folhas_resposta_lookup ON folhas_resposta (professor_id, avaliacao_id, aluno_id, prova_id)",
    "CREATE INDEX IF NOT EXISTS idx_resultados_prof ON resultados (professor_id, prova_id, criado_em)",
    "CREATE INDEX IF NOT EXISTS idx_questoes_cache_busca ON questoes_cache (professor_id, materia, tema, modelo, aprovado)",
    "CREATE INDEX IF NOT EXISTS idx_tarefas_ia_status ON tarefas_ia (status, criado_em)",
    "CREATE INDEX IF NOT EXISTS idx_sessoes_scan_status ON sessoes_scan (professor_id, status, criado_em)",
    "CREATE INDEX IF NOT EXISTS idx_leituras_scan_sessao ON leituras_scan (sessao_id, aluno_id, prova_id)",
    "CREATE INDEX IF NOT EXISTS idx_anotacoes_prof ON anotacoes (professor_id, importante DESC, atualizado_em DESC, criado_em DESC)",
    "CREATE INDEX IF NOT EXISTS idx_diario_prof_data ON diario_bordo (professor_id, data DESC, id DESC)",
    "CREATE INDEX IF NOT EXISTS idx_checklist_prof ON checklist_tcc (professor_id, categoria, ordem, id)",
    "CREATE INDEX IF NOT EXISTS idx_cronograma_prof_status ON cronograma_tarefas (professor_id, status, prazo, id)",
    "CREATE INDEX IF NOT EXISTS idx_anuladas_avaliacao ON questoes_anuladas (professor_id, avaliacao_id, questao)",
    "CREATE INDEX IF NOT EXISTS idx_listas_prof ON listas_exercicios (professor_id, criado_em DESC)",
    "CREATE INDEX IF NOT EXISTS idx_ident_pendentes_prof ON identificacoes_pendentes (professor_id, status, criado_em DESC)",
    "CREATE INDEX IF NOT EXISTS idx_arquivos_ano_prof ON arquivos_ano_letivo (professor_id, ano_letivo)",
]


def iniciar_banco():
    """Cria/atualiza o banco sem apagar dados antigos."""
    if USAR_POSTGRES:
        _iniciar_banco_postgres()
    else:
        _iniciar_banco_sqlite()


def _iniciar_banco_postgres():
    conn = conectar()
    try:
        for ddl in POSTGRES_SCHEMA:
            conn.execute(ddl)

        # Migrações seguras para bancos PostgreSQL antigos.
        for tabela in ["alunos", "avaliacoes", "provas", "folhas_resposta", "resultados", "questoes_cache", "tarefas_ia", "sessoes_scan", "anotacoes", "diario_bordo", "checklist_tcc", "cronograma_tarefas", "questoes_anuladas", "listas_exercicios", "identificacoes_pendentes", "arquivos_ano_letivo"]:
            _adicionar_coluna_se_nao_existir(conn, tabela, "professor_id", "INTEGER")
        _adicionar_coluna_se_nao_existir(conn, "provas", "avaliacao_id", "INTEGER")
        _adicionar_coluna_se_nao_existir(conn, "provas", "qr_arquivo", "TEXT")
        _adicionar_coluna_se_nao_existir(conn, "provas", "tipo_prova", "TEXT DEFAULT 'A'")
        _adicionar_coluna_se_nao_existir(conn, "provas", "mapa_alternativas_json", "TEXT DEFAULT '{}'")
        _adicionar_coluna_se_nao_existir(conn, "resultados", "aluno_id", "INTEGER")
        _adicionar_coluna_se_nao_existir(conn, "resultados", "status_confianca", "TEXT DEFAULT 'confiavel'")
        _adicionar_coluna_se_nao_existir(conn, "avaliacoes", "status_revisao", "TEXT DEFAULT 'rascunho'")
        _adicionar_coluna_se_nao_existir(conn, "avaliacoes", "atualizado_em", "TEXT")
        _adicionar_coluna_se_nao_existir(conn, "questoes_cache", "habilidade", "TEXT")
        _adicionar_coluna_se_nao_existir(conn, "sessoes_scan", "finalizada_em", "TEXT")
        _adicionar_coluna_se_nao_existir(conn, "sessoes_scan", "finalizado_em", "TEXT")
        for tabela in ["alunos", "avaliacoes", "provas", "resultados", "questoes_cache", "anotacoes", "diario_bordo", "checklist_tcc", "cronograma_tarefas"]:
            _adicionar_coluna_se_nao_existir(conn, tabela, "ano_letivo", "TEXT")
            _adicionar_coluna_se_nao_existir(conn, tabela, "arquivado", "INTEGER DEFAULT 0")

        professor_padrao = _garantir_professor_padrao(conn)
        for tabela in ["alunos", "avaliacoes", "provas", "folhas_resposta", "resultados", "questoes_cache", "tarefas_ia", "sessoes_scan", "anotacoes", "diario_bordo", "checklist_tcc", "cronograma_tarefas", "questoes_anuladas", "listas_exercicios", "identificacoes_pendentes", "arquivos_ano_letivo"]:
            if _tabela_existe(conn, tabela) and "professor_id" in _colunas_tabela(conn, tabela):
                conn.execute(f"UPDATE {tabela} SET professor_id = ? WHERE professor_id IS NULL", (professor_padrao,))

        for idx in INDICES:
            conn.execute(idx)

        if _tabela_existe(conn, "folhas_resposta"):
            conn.execute(
                """
                INSERT OR IGNORE INTO folhas_resposta
                    (professor_id, avaliacao_id, prova_id, aluno_id, tipo_prova, mapa_alternativas_json, criado_em)
                SELECT professor_id, avaliacao_id, id, aluno_id,
                       COALESCE(tipo_prova, 'A'),
                       COALESCE(mapa_alternativas_json, '{}'),
                       COALESCE(criado_em, CURRENT_TIMESTAMP::TEXT)
                FROM provas
                """
            )

        alunos = [
            ("Ana Clara Souza", "MAT001", "3º Ano A"),
            ("Bruno Henrique Lima", "MAT002", "3º Ano A"),
            ("Carlos Eduardo Rocha", "MAT003", "3º Ano B"),
        ]
        for nome, matricula, turma in alunos:
            conn.execute(
                """
                INSERT OR IGNORE INTO alunos (professor_id, nome, matricula, turma, criado_em)
                VALUES (?, ?, ?, ?, ?)
                """,
                (professor_padrao, nome, matricula, turma, _agora()),
            )
        conn.commit()
    except Exception:
        conn.rollback()
        raise
    finally:
        conn.close()


def _iniciar_banco_sqlite():
    conn = conectar()
    try:
        # Mantém exatamente o modelo SQLite original para uso local.
        conn.execute("""
            CREATE TABLE IF NOT EXISTS professores (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                nome TEXT NOT NULL,
                email TEXT NOT NULL UNIQUE,
                senha_hash TEXT NOT NULL,
                criado_em TEXT NOT NULL
            )
        """)
        conn.execute("""
            CREATE TABLE IF NOT EXISTS alunos (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                professor_id INTEGER,
                nome TEXT NOT NULL,
                matricula TEXT NOT NULL,
                turma TEXT NOT NULL,
                criado_em TEXT,
                ano_letivo TEXT,
                arquivado INTEGER DEFAULT 0,
                UNIQUE(professor_id, matricula),
                FOREIGN KEY (professor_id) REFERENCES professores(id)
            )
        """)
        conn.execute("""
            CREATE TABLE IF NOT EXISTS avaliacoes (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                professor_id INTEGER,
                titulo TEXT NOT NULL,
                materias TEXT NOT NULL,
                temas TEXT,
                total_questoes INTEGER NOT NULL,
                especificacoes TEXT,
                questoes_json TEXT NOT NULL,
                gabarito_json TEXT NOT NULL,
                status_revisao TEXT DEFAULT 'rascunho',
                criado_em TEXT NOT NULL,
                atualizado_em TEXT,
                ano_letivo TEXT,
                arquivado INTEGER DEFAULT 0,
                FOREIGN KEY (professor_id) REFERENCES professores(id)
            )
        """)
        conn.execute("""
            CREATE TABLE IF NOT EXISTS provas (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                professor_id INTEGER,
                avaliacao_id INTEGER,
                titulo TEXT NOT NULL,
                disciplina TEXT NOT NULL,
                aluno_id INTEGER NOT NULL,
                total_questoes INTEGER NOT NULL,
                gabarito_json TEXT NOT NULL,
                qr_arquivo TEXT,
                tipo_prova TEXT DEFAULT 'A',
                mapa_alternativas_json TEXT DEFAULT '{}',
                criado_em TEXT NOT NULL,
                ano_letivo TEXT,
                arquivado INTEGER DEFAULT 0,
                FOREIGN KEY (professor_id) REFERENCES professores(id),
                FOREIGN KEY (avaliacao_id) REFERENCES avaliacoes(id),
                FOREIGN KEY (aluno_id) REFERENCES alunos(id)
            )
        """)
        conn.execute("""
            CREATE TABLE IF NOT EXISTS folhas_resposta (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                professor_id INTEGER,
                avaliacao_id INTEGER,
                prova_id INTEGER NOT NULL UNIQUE,
                aluno_id INTEGER NOT NULL,
                tipo_prova TEXT DEFAULT 'A',
                mapa_alternativas_json TEXT DEFAULT '{}',
                criado_em TEXT NOT NULL,
                FOREIGN KEY (professor_id) REFERENCES professores(id),
                FOREIGN KEY (avaliacao_id) REFERENCES avaliacoes(id),
                FOREIGN KEY (prova_id) REFERENCES provas(id),
                FOREIGN KEY (aluno_id) REFERENCES alunos(id)
            )
        """)
        conn.execute("""
            CREATE TABLE IF NOT EXISTS resultados (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                professor_id INTEGER,
                aluno_id INTEGER,
                prova_id INTEGER NOT NULL,
                nota_percentual REAL NOT NULL,
                resultado_json TEXT NOT NULL,
                status_confianca TEXT DEFAULT 'confiavel',
                criado_em TEXT NOT NULL,
                ano_letivo TEXT,
                arquivado INTEGER DEFAULT 0,
                FOREIGN KEY (professor_id) REFERENCES professores(id),
                FOREIGN KEY (aluno_id) REFERENCES alunos(id),
                FOREIGN KEY (prova_id) REFERENCES provas(id)
            )
        """)
        conn.execute("""
            CREATE TABLE IF NOT EXISTS questoes_cache (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                professor_id INTEGER,
                materia TEXT NOT NULL,
                tema TEXT,
                dificuldade TEXT DEFAULT 'medio',
                modelo TEXT DEFAULT 'ENEM',
                contexto TEXT,
                enunciado TEXT NOT NULL,
                alternativas_json TEXT NOT NULL,
                correta TEXT NOT NULL,
                habilidade TEXT,
                explicacao TEXT,
                origem TEXT DEFAULT 'ia',
                hash TEXT UNIQUE,
                aprovado INTEGER DEFAULT 1,
                usado_vezes INTEGER DEFAULT 0,
                criado_em TEXT NOT NULL,
                atualizado_em TEXT NOT NULL,
                ano_letivo TEXT,
                arquivado INTEGER DEFAULT 0,
                FOREIGN KEY (professor_id) REFERENCES professores(id)
            )
        """)
        conn.execute("""
            CREATE TABLE IF NOT EXISTS tarefas_ia (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                tipo TEXT NOT NULL,
                status TEXT NOT NULL DEFAULT 'pendente',
                professor_id INTEGER,
                dados_json TEXT NOT NULL,
                resultado_id INTEGER,
                erro TEXT,
                progresso INTEGER DEFAULT 0,
                mensagem TEXT,
                criado_em TEXT NOT NULL,
                atualizado_em TEXT NOT NULL,
                FOREIGN KEY (professor_id) REFERENCES professores(id)
            )
        """)
        conn.execute("""
            CREATE TABLE IF NOT EXISTS sessoes_scan (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                professor_id INTEGER,
                prova_base_id INTEGER,
                modo TEXT NOT NULL DEFAULT 'individual',
                status TEXT NOT NULL DEFAULT 'aberta',
                total_processados INTEGER DEFAULT 0,
                total_sucesso INTEGER DEFAULT 0,
                total_revisao INTEGER DEFAULT 0,
                tempo_medio_ms REAL DEFAULT 0,
                criado_em TEXT NOT NULL,
                finalizado_em TEXT,
                finalizada_em TEXT,
                FOREIGN KEY (professor_id) REFERENCES professores(id)
            )
        """)
        conn.execute("""
            CREATE TABLE IF NOT EXISTS leituras_scan (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                sessao_id INTEGER NOT NULL,
                aluno_id INTEGER,
                prova_id INTEGER NOT NULL,
                resultado_id INTEGER,
                nota_percentual REAL,
                latencia_ms REAL,
                status TEXT NOT NULL,
                mensagem TEXT,
                criado_em TEXT NOT NULL,
                FOREIGN KEY (sessao_id) REFERENCES sessoes_scan(id),
                FOREIGN KEY (aluno_id) REFERENCES alunos(id),
                FOREIGN KEY (prova_id) REFERENCES provas(id),
                FOREIGN KEY (resultado_id) REFERENCES resultados(id)
            )
        """)
        conn.execute("""
            CREATE TABLE IF NOT EXISTS anotacoes (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                professor_id INTEGER NOT NULL,
                titulo TEXT NOT NULL,
                conteudo TEXT NOT NULL,
                categoria TEXT DEFAULT 'Geral',
                avaliacao_id INTEGER,
                turma TEXT,
                importante INTEGER DEFAULT 0,
                criado_em TEXT NOT NULL,
                atualizado_em TEXT,
                ano_letivo TEXT,
                arquivado INTEGER DEFAULT 0,
                FOREIGN KEY (professor_id) REFERENCES professores(id),
                FOREIGN KEY (avaliacao_id) REFERENCES avaliacoes(id)
            )
        """)
        conn.execute("""
            CREATE TABLE IF NOT EXISTS diario_bordo (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                professor_id INTEGER NOT NULL,
                data TEXT NOT NULL,
                atividade TEXT NOT NULL,
                responsavel TEXT,
                status TEXT DEFAULT 'feito',
                observacoes TEXT,
                criado_em TEXT NOT NULL,
                atualizado_em TEXT,
                ano_letivo TEXT,
                arquivado INTEGER DEFAULT 0,
                FOREIGN KEY (professor_id) REFERENCES professores(id)
            )
        """)
        conn.execute("""
            CREATE TABLE IF NOT EXISTS checklist_tcc (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                professor_id INTEGER NOT NULL,
                titulo TEXT NOT NULL,
                categoria TEXT DEFAULT 'TCC',
                concluido INTEGER DEFAULT 0,
                ordem INTEGER DEFAULT 0,
                criado_em TEXT NOT NULL,
                atualizado_em TEXT,
                ano_letivo TEXT,
                arquivado INTEGER DEFAULT 0,
                FOREIGN KEY (professor_id) REFERENCES professores(id)
            )
        """)
        conn.execute("""
            CREATE TABLE IF NOT EXISTS cronograma_tarefas (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                professor_id INTEGER NOT NULL,
                titulo TEXT NOT NULL,
                descricao TEXT,
                responsavel TEXT,
                status TEXT DEFAULT 'afazer',
                prioridade TEXT DEFAULT 'media',
                prazo TEXT,
                criado_em TEXT NOT NULL,
                atualizado_em TEXT,
                ano_letivo TEXT,
                arquivado INTEGER DEFAULT 0,
                FOREIGN KEY (professor_id) REFERENCES professores(id)
            )
        """)
        conn.execute("""
            CREATE TABLE IF NOT EXISTS questoes_anuladas (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                professor_id INTEGER NOT NULL,
                avaliacao_id INTEGER NOT NULL,
                questao INTEGER NOT NULL,
                motivo TEXT,
                criado_em TEXT NOT NULL,
                UNIQUE(professor_id, avaliacao_id, questao),
                FOREIGN KEY (professor_id) REFERENCES professores(id),
                FOREIGN KEY (avaliacao_id) REFERENCES avaliacoes(id)
            )
        """)
        conn.execute("""
            CREATE TABLE IF NOT EXISTS listas_exercicios (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                professor_id INTEGER NOT NULL,
                titulo TEXT NOT NULL,
                filtros_json TEXT DEFAULT '{}',
                questoes_json TEXT NOT NULL,
                incluir_gabarito INTEGER DEFAULT 0,
                arquivo_pdf TEXT,
                criado_em TEXT NOT NULL,
                FOREIGN KEY (professor_id) REFERENCES professores(id)
            )
        """)
        conn.execute("""
            CREATE TABLE IF NOT EXISTS identificacoes_pendentes (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                professor_id INTEGER NOT NULL,
                avaliacao_id INTEGER,
                prova_base_id INTEGER,
                imagem_arquivo TEXT,
                texto_detectado TEXT,
                sugestoes_json TEXT DEFAULT '[]',
                status TEXT DEFAULT 'pendente',
                aluno_id_confirmado INTEGER,
                criado_em TEXT NOT NULL,
                atualizado_em TEXT,
                FOREIGN KEY (professor_id) REFERENCES professores(id),
                FOREIGN KEY (avaliacao_id) REFERENCES avaliacoes(id),
                FOREIGN KEY (prova_base_id) REFERENCES provas(id),
                FOREIGN KEY (aluno_id_confirmado) REFERENCES alunos(id)
            )
        """)
        conn.execute("""
            CREATE TABLE IF NOT EXISTS arquivos_ano_letivo (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                professor_id INTEGER NOT NULL,
                ano_letivo TEXT NOT NULL,
                descricao TEXT,
                totais_json TEXT DEFAULT '{}',
                criado_em TEXT NOT NULL,
                FOREIGN KEY (professor_id) REFERENCES professores(id)
            )
        """)

        for tabela in ["alunos", "avaliacoes", "provas", "folhas_resposta", "resultados", "questoes_cache", "tarefas_ia", "sessoes_scan", "anotacoes", "diario_bordo", "checklist_tcc", "cronograma_tarefas", "questoes_anuladas", "listas_exercicios", "identificacoes_pendentes", "arquivos_ano_letivo"]:
            _adicionar_coluna_se_nao_existir(conn, tabela, "professor_id", "INTEGER")
        _adicionar_coluna_se_nao_existir(conn, "provas", "avaliacao_id", "INTEGER")
        _adicionar_coluna_se_nao_existir(conn, "provas", "qr_arquivo", "TEXT")
        _adicionar_coluna_se_nao_existir(conn, "provas", "tipo_prova", "TEXT DEFAULT 'A'")
        _adicionar_coluna_se_nao_existir(conn, "provas", "mapa_alternativas_json", "TEXT DEFAULT '{}'")
        _adicionar_coluna_se_nao_existir(conn, "resultados", "aluno_id", "INTEGER")
        _adicionar_coluna_se_nao_existir(conn, "resultados", "status_confianca", "TEXT DEFAULT 'confiavel'")
        _adicionar_coluna_se_nao_existir(conn, "avaliacoes", "status_revisao", "TEXT DEFAULT 'rascunho'")
        _adicionar_coluna_se_nao_existir(conn, "avaliacoes", "atualizado_em", "TEXT")
        _adicionar_coluna_se_nao_existir(conn, "questoes_cache", "habilidade", "TEXT")
        _adicionar_coluna_se_nao_existir(conn, "sessoes_scan", "finalizada_em", "TEXT")
        _adicionar_coluna_se_nao_existir(conn, "sessoes_scan", "finalizado_em", "TEXT")
        for tabela in ["alunos", "avaliacoes", "provas", "resultados", "questoes_cache", "anotacoes", "diario_bordo", "checklist_tcc", "cronograma_tarefas"]:
            _adicionar_coluna_se_nao_existir(conn, tabela, "ano_letivo", "TEXT")
            _adicionar_coluna_se_nao_existir(conn, tabela, "arquivado", "INTEGER DEFAULT 0")

        professor_padrao = _garantir_professor_padrao(conn)
        for tabela in ["alunos", "avaliacoes", "provas", "folhas_resposta", "resultados", "questoes_cache", "tarefas_ia", "sessoes_scan", "anotacoes", "diario_bordo", "checklist_tcc", "cronograma_tarefas", "questoes_anuladas", "listas_exercicios", "identificacoes_pendentes", "arquivos_ano_letivo"]:
            if _tabela_existe(conn, tabela) and "professor_id" in _colunas_tabela(conn, tabela):
                conn.execute(f"UPDATE {tabela} SET professor_id = ? WHERE professor_id IS NULL", (professor_padrao,))
        for idx in INDICES:
            conn.execute(idx)

        if _tabela_existe(conn, "folhas_resposta"):
            conn.execute("""
                INSERT OR IGNORE INTO folhas_resposta
                    (professor_id, avaliacao_id, prova_id, aluno_id, tipo_prova, mapa_alternativas_json, criado_em)
                SELECT professor_id, avaliacao_id, id, aluno_id,
                       COALESCE(tipo_prova, 'A'),
                       COALESCE(mapa_alternativas_json, '{}'),
                       COALESCE(criado_em, datetime('now'))
                FROM provas
            """)

        alunos = [
            ("Ana Clara Souza", "MAT001", "3º Ano A"),
            ("Bruno Henrique Lima", "MAT002", "3º Ano A"),
            ("Carlos Eduardo Rocha", "MAT003", "3º Ano B"),
        ]
        for nome, matricula, turma in alunos:
            conn.execute("""
                INSERT OR IGNORE INTO alunos (professor_id, nome, matricula, turma, criado_em)
                VALUES (?, ?, ?, ?, datetime('now'))
            """, (professor_padrao, nome, matricula, turma))

        conn.commit()
    finally:
        conn.close()


def listar_tabelas():
    conn = conectar()
    try:
        return [
            linha[0]
            for linha in conn.execute(
                "SELECT name FROM sqlite_master WHERE type='table' ORDER BY name"
            ).fetchall()
        ]
    finally:
        conn.close()
