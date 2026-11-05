#pragma once

#include <QJsonObject>
#include <QMainWindow>

class Api;
class CodeHighlighter;
class CodeView;
class QCheckBox;
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

protected:
    void changeEvent(QEvent *event) override;

private:
    void syncHighlightTheme();
    void loadMe();
    void loadRepos();
    void openRepo(const QString &repo);
    void browse(const QString &path);
    void loadLog();
    void showFile(const QJsonObject &file);
    QWidget *buildSettings();
    void fillSettings(const QJsonObject &info);
    void saveSettings();
    void deleteRepo();
    void createRepo();
    QString shortName(const QString &repo) const;
    void setBreadcrumb(const QString &path, bool isFile);
    void updateHash();
    void status(const QString &text, bool error = false);
    void showRepoError(const QString &repo, int httpStatus, const QString &error);
    QString ref() const;

    Api *m_api;
    QString m_repo;       // "<owner>/<repo>"
    QString m_owner;      // signed-in account's handle: the namespace new repos go into
    QString m_path;       // current path inside the repo
    QString m_pendingPath;
    QString m_pendingLines;  // "L12" or "L12-L20" from the page URL, applied once that file opens
    bool m_signedIn = false;
    QJsonObject m_info;   // last /repos/<p>/<r> response

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
    CodeView *m_viewer;
    CodeHighlighter *m_highlighter;
    QTreeWidget *m_commits;
    QWidget *m_settings;
    QCheckBox *m_public;
    QLineEdit *m_editDesc;
    QComboBox *m_defaultBranch;
    QLineEdit *m_editName;
    QLabel *m_ownerPrefix = nullptr;
    QWidget *m_newRow;
    QLineEdit *m_newName;
    QPushButton *m_create;
    QPushButton *m_save;
    QLineEdit *m_confirmDelete;
    QPushButton *m_delete;
    QStackedWidget *m_main;
    QJsonObject m_lastError;  // {repo, status, error} of the error page, redrawn once we know who's signed in
    QLabel *m_errIcon;
    QLabel *m_errTitle;
    QLabel *m_errText;
    QPushButton *m_errSignIn;
    QPushButton *m_errRetry;
    QLabel *m_status;
};
