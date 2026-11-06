#pragma once

#include <QHash>
#include <QJsonObject>
#include <QPlainTextEdit>
#include <QTextCursor>
#include <QWidget>

#include <functional>

class Api;
class CodeHighlighter;
class NavPanel;
class QComboBox;
class QLabel;
class QPushButton;
class QSplitter;
class QTimer;
class QToolButton;

// A commit's unified diff: file headers, hunk headers and +/- lines with coloured backgrounds, a
// gutter with the old and new line numbers, and each file's code syntax-highlighted for its language.
// Filled from GET /api/forge/repos/<p>/<r>/commits/<sha>.
class DiffText : public QPlainTextEdit {
public:
    explicit DiffText(QWidget *parent = nullptr);
    ~DiffText() override;

    void setDiff(const QJsonObject &commit);
    // Block number of each file's header, in the order of the commit's "files".
    const QList<int> &fileBlocks() const { return m_fileBlocks; }
    void scrollToBlock(int block);

    // A place in the diff's code: which file (index into "files"), which side (old = the parent's
    // version, for removed lines), the 1-based line on that side and the 0-based column in code points.
    struct Place { int file = -1; bool oldSide = false; int line = 0; int col = 0; QString word; };
    // A click on a name (not a drag-select), and the mouse resting on one.
    std::function<void(const Place &place)> onWordClicked;
    std::function<void(const Place &place, const QPoint &globalPos)> onWordHover;
    // Files whose language has code navigation on the server (see fileapi/codenav.py).
    static bool navigable(const QString &path);
    void setHoverEnabled(bool on) { m_hover = on; }

    int gutterWidth() const;
    void paintGutter(QPaintEvent *event);

    enum Kind : quint8 { FileHeader, HunkHeader, Context, Added, Removed, Note, Spacer };
    struct Line { Kind kind; int oldNo; int newNo; int file; };
    const QList<Line> &lines() const { return m_lines; }
    CodeHighlighter *engineFor(int file) const { return m_fileEngine.value(file); }
    // A hunk can start inside a comment or string that opened above it. Given one side's whole file,
    // works out the highlighter state at the start of each of its lines and re-colours that file.
    void primeFile(int file, bool oldSide, const QByteArray &content);
    // The highlighter state at the start of a line (1-based) of one side of a file; -1 = unknown.
    int stateAt(int file, bool oldSide, int line) const;

protected:
    void resizeEvent(QResizeEvent *event) override;
    void changeEvent(QEvent *event) override;
    void mousePressEvent(QMouseEvent *event) override;
    void mouseReleaseEvent(QMouseEvent *event) override;
    void mouseMoveEvent(QMouseEvent *event) override;
    void leaveEvent(QEvent *event) override;

private:
    void render();
    QColor background(Kind kind) const;
    bool placeAt(const QPoint &pos, Place *place, QTextCursor *word = nullptr) const;

    QWidget *m_gutter;
    class DiffHighlighter *m_highlighter;
    QHash<QString, CodeHighlighter *> m_engines;  // one per language, shared by that language's files
    QList<CodeHighlighter *> m_fileEngine;        // per file (null: plain)
    QList<bool> m_fileNav;                        // per file: code navigation available
    QHash<qint64, QVector<quint8>> m_states;      // (file << 1 | oldSide) -> state at each line start
    QString m_statesFor;                          // the commit m_states belong to
    QJsonObject m_commit;
    QList<Line> m_lines;   // one per block
    QList<int> m_fileBlocks;
    int m_digits = 3;
    bool m_hover = true;
    bool m_pressed = false;
    QPoint m_pressPos;
    QTimer *m_hoverTimer;
    QPoint m_hoverPos;
    QTextCursor m_hoverWord;
};

// The Commits tab's detail page: the commit's message, author and parent, a jump-to-file list, its
// diff, and (for languages the server understands) a definitions/references panel for clicked names.
class CommitView : public QWidget {
    Q_OBJECT
public:
    explicit CommitView(Api *api, QWidget *parent = nullptr);

    void setRepo(const QString &repo);
    // Loads and shows a commit (a full sha or a prefix).
    void showCommit(const QString &sha);
    // The commit asked for: as given until it loads, then the full sha.
    QString sha() const { return m_sha; }
    void setCompact(bool compact);

signals:
    void back();
    void browseFiles(const QString &sha);
    void openCommit(const QString &sha);
    // Open a file at a commit, at line (1-based) and columns col..endCol (code points).
    void openPlace(const QString &ref, const QString &path, int line, int col, int endCol);
    void statusText(const QString &text, bool error);

private:
    void fill(const QJsonObject &commit);
    void setBodyExpanded(bool expanded);
    void fillStats();
    // Where a diff place lives: (ref, path) of that side of the file.
    QPair<QString, QString> source(const DiffText::Place &p) const;
    void lookup(const DiffText::Place &p);
    void hover(const DiffText::Place &p, const QPoint &globalPos);
    void closeNav();
    // Fetches each code file's two versions so hunks that start inside a comment or string colour right.
    void primeHighlighting();

    Api *m_api;
    QString m_repo;
    QString m_sha;
    int m_seq = 0;
    int m_navSeq = 0;
    int m_hoverSeq = 0;
    QString m_navRef;  // the commit the panel's results are from
    QList<QJsonObject> m_hoverCache;
    QString m_body;
    QJsonObject m_commit;
    bool m_compact = false;
    QLabel *m_title;
    QLabel *m_bodyLabel;
    QToolButton *m_more;
    QLabel *m_meta;
    QComboBox *m_files;
    QLabel *m_stats;
    QPushButton *m_browse;
    QSplitter *m_split;
    DiffText *m_diff;
    NavPanel *m_nav;
};
