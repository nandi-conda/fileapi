#pragma once

#include <QPlainTextEdit>

#include <functional>

// Read-only code viewer: a QPlainTextEdit with a line-number gutter on the left.
class CodeView : public QPlainTextEdit {
public:
    explicit CodeView(QWidget *parent = nullptr);

    int gutterWidth() const;
    void paintGutter(QPaintEvent *event);
    void gutterClicked(QMouseEvent *event);

    // Marks lines first..last (1-based, inclusive; 0 clears) and optionally scrolls them into view.
    void setMarkedLines(int first, int last, bool scroll);
    int markedFirst() const { return m_first; }
    int markedLast() const { return m_last; }
    // Called when the user clicks (shift-click extends) a line number.
    std::function<void(int first, int last)> onLinesClicked;

    // Code navigation. Positions are 1-based lines and 0-based columns in UTF-16 units (QString indexes).
    // A click on a word (not a drag-select); `jump` is true for Ctrl/Cmd-click (go straight to the definition).
    std::function<void(int line, int col, bool jump)> onWordClicked;
    // The mouse rested on a word: show hover info near globalPos.
    std::function<void(int line, int col, const QPoint &globalPos)> onWordHover;
    // F12 / Shift+F12 at the text cursor.
    std::function<void(int line, int col, bool references)> onNavKey;
    void setNavEnabled(bool on);
    // Underlines the symbol's occurrences in this file: {line, col, endCol} triples.
    struct Span { int line, col, endCol; };
    void setSymbolMarks(const QList<Span> &spans);
    // Selects a range (jump target) and scrolls it about a third of the way down.
    void revealSpan(int line, int col, int endCol);

protected:
    void resizeEvent(QResizeEvent *event) override;
    void changeEvent(QEvent *event) override;
    void mousePressEvent(QMouseEvent *event) override;
    void mouseReleaseEvent(QMouseEvent *event) override;
    void mouseMoveEvent(QMouseEvent *event) override;
    void leaveEvent(QEvent *event) override;
    void keyPressEvent(QKeyEvent *event) override;

private:
    void updateGutterWidth();
    void updateGutter(const QRect &rect, int dy);

    void applyMarks();
    // The word under a viewport point, as (line, col) of the character; false over blank space.
    bool wordAt(const QPoint &pos, int *line, int *col, QTextCursor *word = nullptr) const;
    void scrollToLine(int line);

    QWidget *m_gutter;
    int m_first = 0;
    int m_last = 0;
    int m_anchor = 0;  // line a shift-click extends from
    bool m_nav = false;
    QPoint m_pressPos;
    bool m_pressed = false;
    QList<Span> m_symbols;
    class QTimer *m_hoverTimer;
    QPoint m_hoverPos;
    QTextCursor m_hoverWord;
};
