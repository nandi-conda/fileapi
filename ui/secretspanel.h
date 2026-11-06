// A write-only secrets list (names and dates, never values) with add/replace/delete, for one API path:
// a repo's ("repos/<owner>/<repo>/secrets") or the signed-in user's ("user/secrets").
#pragma once

#include <QGroupBox>

class Api;
class QBoxLayout;
class QLabel;
class QLineEdit;
class QPushButton;
class QTreeWidget;

class SecretsPanel : public QGroupBox {
    Q_OBJECT
public:
    SecretsPanel(Api *api, const QString &title, const QString &note, QWidget *parent = nullptr);

    // The secrets collection to show, e.g. "user/secrets"; empty = nothing (cleared).
    void setPath(const QString &path);
    QString path() const { return m_path; }
    void load();
    void clear();
    QLabel *noteLabel() const { return m_note; }
    // Phone layout: name, value and buttons stacked instead of in one row.
    void setCompact(bool compact);

signals:
    void statusText(const QString &text, bool error);

private:
    void save();
    void remove();

    Api *m_api;
    QString m_path;
    QLabel *m_note;
    QTreeWidget *m_list;
    QLineEdit *m_name;
    QLineEdit *m_value;
    QPushButton *m_save;
    QPushButton *m_delete;
    QBoxLayout *m_addRow;
};
