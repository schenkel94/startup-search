# Schenkel Startup Search

Schenkel Startup Search e uma ferramenta para centralizar a busca de vagas na area de dados em um so lugar.

A ideia aqui foi unir tres fontes diferentes que ja existiam no projeto, cada uma com suas particularidades, e transformar tudo em uma interface unica em Streamlit, mais simples de usar no dia a dia.

Aplicacao publicada:
[https://startup-search.onrender.com](https://startup-search.onrender.com)

## O que a ferramenta faz

- Busca vagas em fontes como Greenhouse, Gupy e InHire
- Aplica filtros por termos de inclusao e exclusao
- Mostra a origem de cada vaga
- Indica quando a vaga e remota, quando essa informacao existe
- Atualiza os resultados aos poucos, conforme cada fonte termina de carregar
- Permite baixar os resultados em CSV e Excel

## Sobre as fontes

Cada plataforma funciona de um jeito diferente:

- Greenhouse: busca por boards selecionados
- Gupy: busca pela API publica com base nos termos informados
- InHire: usa um scraper dedicado, porque a estrutura dessa fonte exige mais cuidado

No caso do InHire, a informacao de remoto nem sempre esta disponivel na listagem. Quando isso acontece, a ferramenta mostra `N/A` em vez de esconder a vaga.

## Objetivo do projeto

O foco nao e apenas juntar vagas, mas reduzir o atrito da busca.

Em vez de abrir varias ferramentas separadas, a proposta e ter uma visao unica, mais clara, com contexto suficiente para decidir rapido o que vale aplicar.
