#pragma once

#include <QJsonObject>
#include <QMainWindow>

class Api;
class QComboBox;
class QLabel;
class QLineEdit;
class QListWidget;
class QPlainTextEdit;
class QPushButton;
class QStackedWidget;
class QTabWidget;
class QTreeWidget;
class QTreeWidgetItem;

class ForgeWindow : public QMainWindow {
    Q_OBJECT
public:
    explicit ForgeWindow(QWidget *parent = nullptr);

private:
    void loadMe();
    void loadRepos();
    void openRepo(const QString &repo);
    void browse(const QString &path);
    void loadLog();
    void showFile(const QJsonObject &file);
    void setBreadcrumb(const QString &path, bool isFile);
    void updateHash();
    void status(const QString &text, bool error = false);
    QString ref() const;

    Api *m_api;
    QString m_repo;       // "<project>/<repo>"
    QString m_path;       // current path inside the repo
    QString m_pendingPath;

    QLabel *m_who;
    QPushButton *m_signIn;
    QListWidget *m_repos;
    QLabel *m_title;
    QLabel *m_desc;
    QLineEdit *m_clone;
    QComboBox *m_ref;
    QTabWidget *m_tabs;
    QLabel *m_crumbs;
    QStackedWidget *m_filesStack;
    QTreeWidget *m_entries;
    QPlainTextEdit *m_viewer;
    QTreeWidget *m_commits;
    QStackedWidget *m_main;
    QLabel *m_status;
};
