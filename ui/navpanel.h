#pragma once

#include <QJsonObject>
#include <QWidget>

#include <functional>

class QLabel;
class QToolButton;
class QTreeWidget;

// Side panel next to the file viewer: the definitions and references of the symbol last clicked,
// grouped by file. Clicking a row opens that place.
class NavPanel : public QWidget {
public:
    explicit NavPanel(QWidget *parent = nullptr);

    void setLoading(const QString &what);
    void setError(const QString &error);
    // A /symbol response (definitions + references).
    void setResult(const QJsonObject &result);

    // Open `path` at line (1-based), selecting columns col..endCol (server columns: code points).
    std::function<void(const QString &path, int line, int col, int endCol)> onOpen;
    std::function<void()> onClose;
    std::function<void()> onBack;
    void setBackEnabled(bool on);

private:
    QLabel *m_title;
    QLabel *m_sub;
    QToolButton *m_back;
    QTreeWidget *m_tree;
};
