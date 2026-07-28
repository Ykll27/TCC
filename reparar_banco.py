from bd import iniciar_banco, listar_tabelas

if __name__ == "__main__":
    iniciar_banco()
    print("Banco reparado/atualizado com sucesso.")
    print("Tabelas encontradas:")
    for tabela in listar_tabelas():
        print("-", tabela)
