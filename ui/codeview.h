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

protected:
    void resizeEvent(QResizeEvent *event) override;
    void changeEvent(QEvent *event) override;

private:
    void updateGutterWidth();
    void updateGutter(const QRect &rect, int dy);

    void applyMarks();

    QWidget *m_gutter;
    int m_first = 0;
    int m_last = 0;
    int m_anchor = 0;  // line a shift-click extends from
};
