# Regeln für dieses Repo

Das hier ist ein Fork von ByteDance-Seed/Depth-Anything-3.

## Sprache

| Was | Sprache |
|---|---|
| Code, Namen von Variablen und Funktionen | Englisch |
| Kommentare im Code | Englisch |
| Commit-Titel und Commit-Text | Englisch |
| Titel und Text von Pull Requests | Englisch |
| Doku, die wir selbst schreiben (`NEPTUN.md`, eigene Dateien in `docs/`) | Deutsch |
| Chat mit mir | Deutsch |

Ausnahme wegen des Forks: Dateien, die aus dem Original kommen — vor allem
`README.md` und die vorhandenen Dateien in `docs/` — bleiben Englisch. Sonst
gibt es Konflikte, sobald wir das Original wieder hereinholen.

Kurz: Alles, was in einer Code-Datei steht, ist Englisch. Was wir selbst als
Erklärung dazuschreiben, ist Deutsch.

## Wie du mit mir redest

- Einfache Sätze. Ein Gedanke pro Satz.
- Keine erfundenen Wörter und keine Bilder, die man erst entschlüsseln muss.
- Fachbegriffe nur, wenn es keinen deutschen Ausdruck dafür gibt. Dann
  einmal in einem Nebensatz erklären, was er bedeutet.
- Englische Begriffe, die im Projekt sowieso vorkommen (Commit, Branch, Pull
  Request, Modell, Inferenz, Punktwolke), darfst du normal benutzen.
- Sag zuerst, was du geändert hast. Die Begründung kommt danach.
- Wenn etwas nicht funktioniert hat, schreib das hin. Nicht drumherum reden.

## Projekt

Dieser Fork wird für genau eine Sache benutzt: das `da3`-Kommando und das
Modell, die aus ein paar Scan-Bildern eine `scene.glb` machen.
Details in `NEPTUN.md`. Der Code liegt in `src/`.

## Befehle

```bash
pip install -r requirements.txt
pre-commit run --all-files     # Formatierung und Linter, das prüft auch die CI
```

## Arbeitsweise

- So wenig wie möglich am Original ändern. Je kleiner unser Unterschied zum
  Original ist, desto einfacher ist das nächste Update von dort.
- Was wir selbst dazubauen, gehört nach Möglichkeit in eigene Dateien.
- Kleine Änderungen. Eine Sache pro Commit.
