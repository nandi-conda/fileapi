#pragma once

#include <QJsonObject>
#include <QMainWindow>
#include <QUrlQuery>

class Api;
class QAction;
class CodeHighlighter;
class CodeView;
class NavPanel;
class SecretsPanel;
class QBoxLayout;
class QFormLayout;
class QSplitter;
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
class QTimer;

class ForgeWindow : public QMainWindow {
    Q_OBJECT
public:
    explicit ForgeWindow(QWidget *parent = nullptr);

protected:
    void changeEvent(QEvent *event) override;
    void resizeEvent(QResizeEvent *event) override;

private:
    // Phone-width layout: one pane at a time (repo list, or the open repo with a back button), rows
    // that stack instead of sitting side by side, and fewer table columns.
    void setCompact(bool compact);
    void updatePanes();
    void closeRepo();
    void setTabBadge(int tab, const QString &badge);
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
    // Deploy job runs (the Deploys tab). Secrets are SecretsPanels: the repo's in its Settings tab, the
    // user's (every repo they own gets those) on the user settings page.
    void showUserSettings();
    QWidget *buildDeploys();
    void loadRuns();
    void showRun(const QString &id);
    void startRun();
    void createRepo();
    QString shortName(const QString &repo) const;
    void setBreadcrumb(const QString &path, bool isFile);
    void updateHash();
    void status(const QString &text, bool error = false);
    void showRepoError(const QString &repo, int httpStatus, const QString &error);
    QString ref() const;
    // Code navigation (go to definition, references, hover; served by /symbol and /symbols).
    void loadOutline();
    void lookupSymbol(int line, int col, bool jump);
    void hoverSymbol(int line, int col, const QPoint &globalPos);
    void openPlace(const QString &path, int line, int col, int endCol, bool remember = true);
    void goBack();
    void closeNav();
    QUrlQuery symbolQuery(int line, int col) const;

    Api *m_api;
    QString m_repo;       // "<owner>/<repo>"
    QString m_owner;      // signed-in account's handle: the namespace new repos go into
    QString m_path;       // current path inside the repo
    QString m_pendingPath;
    QString m_pendingLines;  // "L12" or "L12-L20" from the page URL, applied once that file opens
    bool m_signedIn = false;
    QJsonObject m_info;   // last /repos/<p>/<r> response
    struct Place { QString path; int line; };
    QList<Place> m_backStack;  // where "Back" returns to after jumping to a definition
    QJsonObject m_pendingSpan; // {line, col, end_col} to select once the file being opened has loaded
    int m_navSeq = 0;          // drops answers to lookups that were superseded
    int m_hoverSeq = 0;
    QList<QJsonObject> m_hoverCache;  // /symbol answers for the open file, reused while hovering

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
    QSplitter *m_fileSplit;
    NavPanel *m_navPanel;
    QComboBox *m_outline;
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
    SecretsPanel *m_repoSecrets;
    SecretsPanel *m_userSecrets;
    QPushButton *m_userSettingsBtn;
    QAction *m_userSettingsAct;
    QWidget *m_deploys;
    QTreeWidget *m_runs;
    QPlainTextEdit *m_runOutput;
    QPushButton *m_runNow;
    QComboBox *m_runBranch;
    QLabel *m_runsNote;
    QTimer *m_runsTimer;
    QString m_runShown;  // id of the run whose output is showing
    QStackedWidget *m_main;
    QJsonObject m_lastError;  // {repo, status, error} of the error page, redrawn once we know who's signed in
    QLabel *m_errIcon;
    QLabel *m_errTitle;
    QLabel *m_errText;
    QPushButton *m_errSignIn;
    QPushButton *m_errRetry;
    QLabel *m_status;

    bool m_compact = false;
    QSplitter *m_split;
    QWidget *m_left;
    QAction *m_whoAction;
    QAction *m_backAction;
    QBoxLayout *m_headRow;
    QBoxLayout *m_crumbRow;
    QBoxLayout *m_errButtons;
    QFormLayout *m_settingsForm;
    QLabel *m_branchLabel;
    QWidget *m_cloneRow;
    QLabel *m_runBranchLabel;
    QPushButton *m_runRefresh;
    class QHBoxLayout *m_titleRow;
    class QHBoxLayout *m_branchRow;
    QBoxLayout *m_repoLayout;
    QString m_commitsBadge;  // tab labels' extras: "40" / "error", and "running"
    QString m_deploysBadge;
};
