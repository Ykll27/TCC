import os
import sqlite3
from pathlib import Path

BASE_DIR = Path(__file__).resolve().parent
DB_PATH = Path(os.getenv("DB_PATH", BASE_DIR / "atlas.sqlite3"))
DB_PATH.parent.mkdir(parents=True, exist_ok=True)


def conectar():
    conn = sqlite3.connect(DB_PATH, timeout=30, check_same_thread=False)
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA journal_mode=WAL")
    conn.execute("PRAGMA busy_timeout=30000")
    conn.execute("PRAGMA foreign_keys=ON")
    return conn


def _tabela_existe(conn, tabela):
    row = conn.execute(
        "SELECT name FROM sqlite_master WHERE type='table' AND name=?",
        (tabela,),
    ).fetchone()
    return row is not None


def _colunas_tabela(conn, tabela):
    if not _tabela_existe(conn, tabela):
        return set()
    return {linha[1] for linha in conn.execute(f"PRAGMA table_info({tabela})").fetchall()}


def _adicionar_coluna_se_nao_existir(conn, tabela, coluna, definicao):
    if not _tabela_existe(conn, tabela):
        return
    colunas = _colunas_tabela(conn, tabela)
    if coluna not in colunas:
        conn.execute(f"ALTER TABLE {tabela} ADD COLUMN {coluna} {definicao}")


def _garantir_professor_padrao(conn):
    row = conn.execute("SELECT id FROM professores ORDER BY id LIMIT 1").fetchone()
    if row:
        return int(row["id"])
    cursor = conn.execute(
        """
        INSERT INTO professores (nome, email, senha_hash, criado_em)
        VALUES (?, ?, ?, datetime('now'))
        """,
        ("Professor Demo", "demo@atlas.local", ""),
    )
    return int(cursor.lastrowid)


def iniciar_banco():
    """
    Cria e atualiza o banco sem apagar dados antigos.

    Módulos incluídos:
    - Login de professores;
    - Alunos e turmas por professor;
    - Criador de provas com cache local e IA mínima;
    - Correção por upload/scan;
    - Relatórios e leitura detalhada;
    - Fila assíncrona para quando a IA for necessária.
    """
    conn = conectar()

    try:
        conn.execute(
            """
            CREATE TABLE IF NOT EXISTS professores (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                nome TEXT NOT NULL,
                email TEXT NOT NULL UNIQUE,
                senha_hash TEXT NOT NULL,
                criado_em TEXT NOT NULL
            )
            """
        )

        conn.execute(
            """
            CREATE TABLE IF NOT EXISTS alunos (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                professor_id INTEGER,
                nome TEXT NOT NULL,
                matricula TEXT NOT NULL UNIQUE,
                turma TEXT NOT NULL,
                criado_em TEXT,
                FOREIGN KEY (professor_id) REFERENCES professores(id)
            )
            """
        )

        conn.execute(
            """
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
                FOREIGN KEY (professor_id) REFERENCES professores(id)
            )
            """
        )

        conn.execute(
            """
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
                criado_em TEXT NOT NULL,
                FOREIGN KEY (professor_id) REFERENCES professores(id),
                FOREIGN KEY (avaliacao_id) REFERENCES avaliacoes(id),
                FOREIGN KEY (aluno_id) REFERENCES alunos(id)
            )
            """
        )

        conn.execute(
            """
            CREATE TABLE IF NOT EXISTS resultados (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                professor_id INTEGER,
                aluno_id INTEGER,
                prova_id INTEGER NOT NULL,
                nota_percentual REAL NOT NULL,
                resultado_json TEXT NOT NULL,
                status_confianca TEXT DEFAULT 'confiavel',
                criado_em TEXT NOT NULL,
                FOREIGN KEY (professor_id) REFERENCES professores(id),
                FOREIGN KEY (aluno_id) REFERENCES alunos(id),
                FOREIGN KEY (prova_id) REFERENCES provas(id)
            )
            """
        )

        conn.execute(
            """
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
                FOREIGN KEY (professor_id) REFERENCES professores(id)
            )
            """
        )

        conn.execute(
            """
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
            """
        )

        conn.execute(
            """
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
                FOREIGN KEY (professor_id) REFERENCES professores(id)
            )
            """
        )

        conn.execute(
            """
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
            """
        )

        conn.execute(
            """
            CREATE TABLE IF NOT EXISTS identificacoes_pendentes (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                professor_id INTEGER NOT NULL,
                sessao_id INTEGER,
                prova_base_id INTEGER,
                imagem_arquivo TEXT NOT NULL,
                origem TEXT NOT NULL DEFAULT 'scan',
                status TEXT NOT NULL DEFAULT 'pendente',
                mensagem TEXT,
                dados_qr_json TEXT,
                sugestoes_json TEXT,
                resultado_id INTEGER,
                criado_em TEXT NOT NULL,
                resolvido_em TEXT,
                FOREIGN KEY (professor_id) REFERENCES professores(id),
                FOREIGN KEY (sessao_id) REFERENCES sessoes_scan(id),
                FOREIGN KEY (prova_base_id) REFERENCES provas(id),
                FOREIGN KEY (resultado_id) REFERENCES resultados(id)
            )
            """
        )

        # Migrações seguras para bancos antigos.
        for tabela in ["alunos", "avaliacoes", "provas", "resultados", "questoes_cache", "tarefas_ia", "sessoes_scan"]:
            _adicionar_coluna_se_nao_existir(conn, tabela, "professor_id", "INTEGER")
        _adicionar_coluna_se_nao_existir(conn, "provas", "avaliacao_id", "INTEGER")
        _adicionar_coluna_se_nao_existir(conn, "provas", "qr_arquivo", "TEXT")
        _adicionar_coluna_se_nao_existir(conn, "provas", "ordem_questoes_json", "TEXT")
        _adicionar_coluna_se_nao_existir(conn, "provas", "mapa_questoes_json", "TEXT")
        _adicionar_coluna_se_nao_existir(conn, "resultados", "aluno_id", "INTEGER")
        _adicionar_coluna_se_nao_existir(conn, "resultados", "status_confianca", "TEXT DEFAULT 'confiavel'")
        _adicionar_coluna_se_nao_existir(conn, "resultados", "revisado_em", "TEXT")
        _adicionar_coluna_se_nao_existir(conn, "resultados", "observacao_revisao", "TEXT")
        _adicionar_coluna_se_nao_existir(conn, "avaliacoes", "status_revisao", "TEXT DEFAULT 'rascunho'")
        _adicionar_coluna_se_nao_existir(conn, "avaliacoes", "atualizado_em", "TEXT")
        _adicionar_coluna_se_nao_existir(conn, "questoes_cache", "habilidade", "TEXT")

        professor_padrao = _garantir_professor_padrao(conn)
        for tabela in ["alunos", "avaliacoes", "provas", "resultados", "questoes_cache", "tarefas_ia", "sessoes_scan"]:
            if _tabela_existe(conn, tabela) and "professor_id" in _colunas_tabela(conn, tabela):
                conn.execute(f"UPDATE {tabela} SET professor_id = ? WHERE professor_id IS NULL", (professor_padrao,))

        conn.execute("CREATE INDEX IF NOT EXISTS idx_alunos_prof_turma ON alunos (professor_id, turma, nome)")
        conn.execute("CREATE INDEX IF NOT EXISTS idx_avaliacoes_prof ON avaliacoes (professor_id, id DESC)")
        conn.execute("CREATE INDEX IF NOT EXISTS idx_provas_prof_avaliacao ON provas (professor_id, avaliacao_id, aluno_id)")
        conn.execute("CREATE INDEX IF NOT EXISTS idx_resultados_prof ON resultados (professor_id, prova_id, criado_em)")
        conn.execute("CREATE INDEX IF NOT EXISTS idx_questoes_cache_busca ON questoes_cache (professor_id, materia, tema, modelo, aprovado)")
        conn.execute("CREATE INDEX IF NOT EXISTS idx_tarefas_ia_status ON tarefas_ia (status, criado_em)")
        conn.execute("CREATE INDEX IF NOT EXISTS idx_sessoes_scan_status ON sessoes_scan (professor_id, status, criado_em)")
        conn.execute("CREATE INDEX IF NOT EXISTS idx_leituras_scan_sessao ON leituras_scan (sessao_id, aluno_id, prova_id)")
        conn.execute("CREATE INDEX IF NOT EXISTS idx_identificacoes_pendentes_prof ON identificacoes_pendentes (professor_id, status, criado_em)")

        alunos = [
            ("Ana Clara Souza", "MAT001", "3º Ano A"),
            ("Bruno Henrique Lima", "MAT002", "3º Ano A"),
            ("Carlos Eduardo Rocha", "MAT003", "3º Ano B"),
        ]
        for nome, matricula, turma in alunos:
            conn.execute(
                """
                INSERT OR IGNORE INTO alunos (professor_id, nome, matricula, turma, criado_em)
                VALUES (?, ?, ?, ?, datetime('now'))
                """,
                (professor_padrao, nome, matricula, turma),
            )

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
