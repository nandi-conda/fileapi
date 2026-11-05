#pragma once

#include <QPlainTextEdit>

// Read-only code viewer: a QPlainTextEdit with a line-number gutter on the left.
class CodeView : public QPlainTextEdit {
public:
    explicit CodeView(QWidget *parent = nullptr);

    int gutterWidth() const;
    void paintGutter(QPaintEvent *event);

protected:
    void resizeEvent(QResizeEvent *event) override;
    void changeEvent(QEvent *event) override;

private:
    void updateGutterWidth();
    void updateGutter(const QRect &rect, int dy);

    QWidget *m_gutter;
};
