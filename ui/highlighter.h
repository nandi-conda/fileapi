#pragma once

#include <QRegularExpression>
#include <QSyntaxHighlighter>
#include <QTextCharFormat>
#include <QVector>

// Small rule-based syntax highlighter for the file viewer. The language comes from the file name
// (extension or well-known names like Makefile); colours follow the viewer's palette, so it works in
// dark and light mode. Deliberately tiny: no external syntax definitions, so the wasm stays small.
class CodeHighlighter : public QSyntaxHighlighter {
public:
    explicit CodeHighlighter(QTextDocument *doc);

    // Picks the language for `fileName` ("" turns highlighting off). Returns the language name or "".
    QString setFileName(const QString &fileName);
    // Re-picks the colours (call when the palette changes between dark and light).
    void setDark(bool dark);

protected:
    void highlightBlock(const QString &text) override;

private:
    enum Kind { Keyword, Type, Constant, String, Comment, Number, Function, Meta, Tag, Attr, Heading,
                Added, Removed, KindCount };
    struct Rule {
        QRegularExpression re;
        Kind kind;
        int group = 0;  // capture group to colour (0 = whole match)
    };
    // A construct that can span lines, e.g. /* ... */ or """ ... """. Its index + 1 is the block state.
    struct Span {
        QRegularExpression start;
        QRegularExpression end;
        Kind kind;
    };

    void buildFormats();

    bool m_dark = false;
    QVector<Rule> m_rules;
    QVector<Span> m_spans;
    QTextCharFormat m_formats[KindCount];
};
