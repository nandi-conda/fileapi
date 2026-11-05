#include "forgewindow.h"
#include "api.h"
#include "codeview.h"
#include "highlighter.h"

#include <QCheckBox>
#include <QComboBox>
#include <QDateTime>
#include <QFontDatabase>
#include <QFormLayout>
#include <QGroupBox>
#include <QHBoxLayout>
#include <QHeaderView>
#include <QJsonArray>
#include <QJsonValue>
#include <QLabel>
#include <QLineEdit>
#include <QListWidget>
#include <QLocale>
#include <QPlainTextEdit>
#include <QProxyStyle>
#include <QRegularExpression>
#include <QPushButton>
#include <QSignalBlocker>
#include <QSplitter>
#include <QStackedWidget>
#include <QStatusBar>
#include <QStyle>
#include <QStyleFactory>
#include <QTabBar>
#include <QTabWidget>
#include <QTimer>
#include <QToolBar>
#include <QTreeWidget>
#include <QVBoxLayout>

namespace {
constexpr int PathRole = Qt::UserRole;
constexpr int DirRole = Qt::UserRole + 1;
constexpr int ShaRole = Qt::UserRole + 2;

QString humanSize(qint64 n)
{
    return QLocale::c().formattedDataSize(n, 1, QLocale::DataSizeTraditionalFormat);
}

QString repoPath(const QString &repo) { return "repos/" + repo; }

// Splits a trailing "#L12" or "#L12-L20" (GitHub-style line link) off a location hash; returns "L12…" or "".
QString splitLines(QString &hash)
{
    static const QRegularExpression re(QStringLiteral("#(L\\d+(?:-L?\\d+)?)$"));
    const auto m = re.match(hash);
    if (!m.hasMatch())
        return {};
    hash.chop(m.capturedLength());
    return m.captured(1);
}

// Opens items on a single click, like links on the web (Enter still works too).
class SingleClickStyle : public QProxyStyle {
public:
    using QProxyStyle::QProxyStyle;
    int styleHint(StyleHint hint, const QStyleOption *opt, const QWidget *w, QStyleHintReturn *ret) const override
    {
        if (hint == SH_ItemView_ActivateItemOnSingleClick)
            return 1;
        return QProxyStyle::styleHint(hint, opt, w, ret);
    }
};
}  // namespace

ForgeWindow::ForgeWindow(QWidget *parent) : QMainWindow(parent), m_api(new Api(this))
{
    setWindowTitle("fileapi forge");

    // ---- top bar
    auto *bar = addToolBar("Account");
    bar->setMovable(false);
    auto *brand = new QLabel("<b>fileapi</b> forge");
    brand->setContentsMargins(8, 0, 16, 0);
    bar->addWidget(brand);
    auto *spacer = new QWidget;
    spacer->setSizePolicy(QSizePolicy::Expanding, QSizePolicy::Preferred);
    bar->addWidget(spacer);
    m_who = new QLabel;
    m_who->setContentsMargins(0, 0, 8, 0);
    bar->addWidget(m_who);
    m_signIn = new QPushButton("Sign in");
    bar->addWidget(m_signIn);
    connect(m_signIn, &QPushButton::clicked, this, [this] {
        m_api->hasToken() ? m_api->signOut() : m_api->signIn();
    });

    // ---- repo list
    auto *split = new QSplitter;
    auto *left = new QWidget;
    auto *leftLay = new QVBoxLayout(left);
    leftLay->setContentsMargins(8, 8, 0, 8);
    leftLay->addWidget(new QLabel("<b>Repositories</b>"));
    m_repos = new QListWidget;
    leftLay->addWidget(m_repos);
    // New repo: only a name is needed; it is created under the signed-in handle.
    m_newRow = new QWidget;
    auto *newLay = new QHBoxLayout(m_newRow);
    newLay->setContentsMargins(0, 0, 0, 0);
    m_newName = new QLineEdit;
    m_newName->setPlaceholderText("new-repo-name");
    newLay->addWidget(m_newName, 1);
    m_create = new QPushButton("Create");
    m_create->setIcon(style()->standardIcon(QStyle::SP_FileDialogNewFolder));
    newLay->addWidget(m_create);
    m_newRow->setVisible(false);
    leftLay->addWidget(m_newRow);
    connect(m_create, &QPushButton::clicked, this, &ForgeWindow::createRepo);
    connect(m_newName, &QLineEdit::returnPressed, this, &ForgeWindow::createRepo);
    split->addWidget(left);
    connect(m_repos, &QListWidget::currentItemChanged, this, [this](QListWidgetItem *it) {
        if (it && it->data(PathRole).toString() != m_repo)
            openRepo(it->data(PathRole).toString());
    });

    // ---- repo view
    m_main = new QStackedWidget;
    auto *empty = new QLabel("Pick a repository on the left.");
    empty->setAlignment(Qt::AlignCenter);
    m_main->addWidget(empty);

    auto *repoView = new QWidget;
    auto *lay = new QVBoxLayout(repoView);
    lay->setContentsMargins(8, 8, 8, 8);
    m_title = new QLabel;
    QFont tf = m_title->font();
    tf.setPointSizeF(tf.pointSizeF() * 1.5);
    tf.setBold(true);
    m_title->setFont(tf);
    m_title->setTextInteractionFlags(Qt::TextSelectableByMouse);
    // The header keeps one fixed size while a repo loads, so nothing below it jumps: the description
    // always takes exactly one line (cut off when long, full text in the tooltip), even when empty.
    m_title->setFixedHeight(QFontMetrics(tf).lineSpacing() + 4);
    m_title->setSizePolicy(QSizePolicy::Ignored, QSizePolicy::Fixed);
    lay->addWidget(m_title);
    m_desc = new QLabel;
    m_desc->setWordWrap(false);
    m_desc->setFixedHeight(m_desc->fontMetrics().lineSpacing() + 2);
    m_desc->setSizePolicy(QSizePolicy::Ignored, QSizePolicy::Fixed);
    lay->addWidget(m_desc);

    auto *row = new QHBoxLayout;
    row->addWidget(new QLabel("Branch:"));
    m_ref = new QComboBox;
    m_ref->setMinimumWidth(160);
    m_ref->setEditable(true);  // also accepts a commit sha or tag typed in
    row->addWidget(m_ref);
    row->addSpacing(16);
    row->addWidget(new QLabel("Clone:"));
    m_clone = new QLineEdit;
    m_clone->setReadOnly(true);
    row->addWidget(m_clone, 1);
    lay->addLayout(row);
    connect(m_ref, &QComboBox::textActivated, this, [this] {
        browse(QString());
        loadLog();
    });

    m_tabs = new QTabWidget;
    // Wide enough for "Commits (100)", so the tabs don't shift when the commit count arrives.
    m_tabs->tabBar()->setStyleSheet(QStringLiteral("QTabBar::tab { min-width: %1px; }")
                                        .arg(m_tabs->fontMetrics().horizontalAdvance("Commits (100)") + 8));
    // Files tab: breadcrumb, then either a directory listing or a file viewer.
    auto *files = new QWidget;
    auto *fl = new QVBoxLayout(files);
    fl->setContentsMargins(4, 4, 4, 4);
    m_crumbs = new QLabel;
    m_crumbs->setTextFormat(Qt::RichText);
    m_crumbs->setFixedHeight(m_crumbs->fontMetrics().lineSpacing() + 6);
    m_crumbs->setSizePolicy(QSizePolicy::Ignored, QSizePolicy::Fixed);
    connect(m_crumbs, &QLabel::linkActivated, this, [this](const QString &link) { browse(link.mid(1)); });
    fl->addWidget(m_crumbs);
    m_filesStack = new QStackedWidget;
    m_entries = new QTreeWidget;
    m_entries->setHeaderLabels({"Name", "Size"});
    m_entries->setRootIsDecorated(false);
    m_entries->setUniformRowHeights(true);
    {
        auto *single = new SingleClickStyle(QStyleFactory::create("Fusion"));
        single->setParent(m_entries);
        m_entries->setStyle(single);
    }
    m_entries->setCursor(Qt::PointingHandCursor);
    m_entries->header()->setStretchLastSection(false);
    m_entries->header()->setSectionResizeMode(0, QHeaderView::Stretch);
    m_entries->header()->setSectionResizeMode(1, QHeaderView::ResizeToContents);
    connect(m_entries, &QTreeWidget::itemActivated, this, [this](QTreeWidgetItem *it) {
        browse(it->data(0, PathRole).toString());
    });
    m_filesStack->addWidget(m_entries);
    m_viewer = new CodeView;
    m_viewer->onLinesClicked = [this](int, int) { updateHash(); };
    m_viewer->setReadOnly(true);
    m_viewer->setLineWrapMode(QPlainTextEdit::NoWrap);
    m_viewer->setFont(QFontDatabase::systemFont(QFontDatabase::FixedFont));
    m_highlighter = new CodeHighlighter(m_viewer->document());
    m_filesStack->addWidget(m_viewer);
    // Highlight colours follow the viewer: dark text-on-light or light-on-dark.
    syncHighlightTheme();  // and again on every palette change, see changeEvent()
    fl->addWidget(m_filesStack, 1);
    m_tabs->addTab(files, "Files");

    m_commits = new QTreeWidget;
    m_commits->setHeaderLabels({"Message", "Author", "Date", "Commit"});
    m_commits->setRootIsDecorated(false);
    m_commits->setUniformRowHeights(true);
    m_commits->header()->setStretchLastSection(false);
    m_commits->header()->setSectionResizeMode(0, QHeaderView::Stretch);
    for (int i = 1; i < 4; ++i)
        m_commits->header()->setSectionResizeMode(i, QHeaderView::ResizeToContents);
    m_commits->setToolTip("Double-click a commit to browse the files as they were then.");
    connect(m_commits, &QTreeWidget::itemActivated, this, [this](QTreeWidgetItem *it) {
        m_ref->setCurrentText(it->data(0, ShaRole).toString().left(12));
        m_tabs->setCurrentIndex(0);
        browse(QString());
        loadLog();
    });
    m_tabs->addTab(m_commits, "Commits");
    connect(m_tabs, &QTabWidget::currentChanged, this, [this](int i) {
        if (i == 1 && !m_repo.isEmpty() && m_ref->count() > 0
            && (m_commits->topLevelItemCount() == 0 || m_tabs->tabText(1).endsWith("(error)")))
            loadLog();
    });
    m_settings = buildSettings();
    m_tabs->addTab(m_settings, "Settings");
    m_tabs->setTabVisible(2, false);  // only the signed-in admin can change settings
    lay->addWidget(m_tabs, 1);
    m_main->addWidget(repoView);

    // ---- error page: a repo that can't be opened. For anyone not signed in, a private repo and a missing
    // one look the same (the server answers 404 for both), so the page never says which it is.
    auto *errPage = new QWidget;
    auto *el = new QVBoxLayout(errPage);
    el->addStretch(1);
    m_errIcon = new QLabel;
    m_errIcon->setAlignment(Qt::AlignCenter);
    el->addWidget(m_errIcon);
    m_errTitle = new QLabel;
    QFont ef = m_errTitle->font();
    ef.setPointSizeF(ef.pointSizeF() * 1.5);
    ef.setBold(true);
    m_errTitle->setFont(ef);
    m_errTitle->setAlignment(Qt::AlignCenter);
    el->addWidget(m_errTitle);
    m_errText = new QLabel;
    m_errText->setWordWrap(true);
    m_errText->setAlignment(Qt::AlignCenter);
    m_errText->setTextInteractionFlags(Qt::TextSelectableByMouse);
    el->addWidget(m_errText);
    el->addSpacing(12);
    auto *eb = new QHBoxLayout;
    eb->addStretch(1);
    m_errSignIn = new QPushButton("Sign in");
    eb->addWidget(m_errSignIn);
    m_errRetry = new QPushButton("Try again");
    m_errRetry->setIcon(style()->standardIcon(QStyle::SP_BrowserReload));
    eb->addWidget(m_errRetry);
    auto *errBack = new QPushButton("Back to repositories");
    errBack->setIcon(style()->standardIcon(QStyle::SP_ArrowBack));
    eb->addWidget(errBack);
    eb->addStretch(1);
    el->addLayout(eb);
    el->addStretch(2);
    connect(m_errSignIn, &QPushButton::clicked, this, [this] { m_api->signIn(); });  // keeps #<repo>, comes back to it
    connect(m_errRetry, &QPushButton::clicked, this, [this] {
        const QString repo = m_repo;
        m_repo.clear();
        openRepo(repo);
    });
    connect(errBack, &QPushButton::clicked, this, [this] {
        m_repo.clear();
        m_pendingPath.clear();
        m_api->setLocationHash(QString());
        const QSignalBlocker block(m_repos);
        m_repos->setCurrentRow(-1);
        m_main->setCurrentIndex(0);
        status(QStringLiteral("%1 repositories").arg(m_repos->count()));
    });
    m_main->addWidget(errPage);
    split->addWidget(m_main);
    split->setStretchFactor(1, 1);
    split->setSizes({260, 900});
    setCentralWidget(split);

    m_status = new QLabel;
    statusBar()->addWidget(m_status, 1);

    loadMe();
    loadRepos();
}

void ForgeWindow::status(const QString &text, bool error)
{
    m_status->setText(text);
    m_status->setStyleSheet(error ? "color: #c62828;" : "");
}

void ForgeWindow::showRepoError(const QString &repo, int httpStatus, const QString &error)
{
    const bool noAccess = httpStatus == 401 || httpStatus == 403 || httpStatus == 404;
    // A repo name that isn't valid at all (400/422) is just as "not there" as a missing one.
    const bool missing = noAccess || httpStatus == 400 || httpStatus == 422;
    m_lastError = {{"repo", repo}, {"status", httpStatus}, {"error", error}};
    m_main->setCurrentIndex(2);
    m_errIcon->setPixmap(style()->standardIcon(missing ? QStyle::SP_MessageBoxWarning : QStyle::SP_MessageBoxCritical)
                             .pixmap(48, 48));
    if (missing) {
        m_errTitle->setText("Repository not found");
        m_errText->setText(m_signedIn
            ? QStringLiteral("<b>%1</b> doesn't exist. It may have been deleted, or the link may be mistyped.")
                  .arg(repo.toHtmlEscaped())
            : QStringLiteral("<b>%1</b> doesn't exist, or it's private and you need to sign in to see it.")
                  .arg(repo.toHtmlEscaped()));
    } else {
        m_errTitle->setText("Couldn't load this repository");
        m_errText->setText(QStringLiteral("Something went wrong loading <b>%1</b>: %2")
                               .arg(repo.toHtmlEscaped(), error.toHtmlEscaped()));
    }
    m_errSignIn->setVisible(missing && !m_signedIn);
    m_errRetry->setVisible(!missing);
    status(missing ? "Not found: " + repo : error, true);
}

QString ForgeWindow::ref() const
{
    const QString r = m_ref->currentText().trimmed();
    return r.isEmpty() ? "HEAD" : r;
}

void ForgeWindow::loadMe()
{
    m_api->get("me", {}, this, [this](const QJsonObject &me, const QString &error) {
        const bool in = error.isEmpty() && me.value("signed_in").toBool();
        const QString admin = me.value("admin").toString();
        m_owner = me.value("owner").toString();
        m_who->setText(in ? "Signed in as " + (m_owner.isEmpty() ? admin : m_owner) : "Public repos only");
        m_signIn->setText(in ? "Sign out" : "Sign in");
        m_signIn->setToolTip(in ? QString() : "Only " + admin + " can sign in, to see private repos.");
        m_signedIn = in;
        m_tabs->setTabVisible(2, in);
        m_newRow->setVisible(in);
        m_newName->setToolTip(QStringLiteral("Creates %1/<name>").arg(m_owner));
        if (m_ownerPrefix)
            m_ownerPrefix->setText(m_owner + " /");
        if (m_main->currentIndex() == 2 && !m_lastError.isEmpty())  // page drawn before we knew: redo the wording
            showRepoError(m_lastError.value("repo").toString(), m_lastError.value("status").toInt(),
                          m_lastError.value("error").toString());
        if (m_repos->count() > 0)
            loadRepos();  // relabel with short names now that we know whose repos they are
    });
}

void ForgeWindow::loadRepos()
{
    status("Loading repositories…");
    m_api->get("repos", {}, this, [this](const QJsonObject &body, const QString &error) {
        if (!error.isEmpty())
            return status("Could not list repositories: " + error, true);
        m_repos->clear();
        const QJsonArray repos = body.value("repos").toArray();
        for (const auto &v : repos) {
            const QJsonObject r = v.toObject();
            const QString name = r.value("repo").toString();
            const bool pub = r.value("public").toBool();
            auto *it = new QListWidgetItem(shortName(name) + (pub ? "" : "  (private)"));
            it->setData(PathRole, name);
            it->setToolTip(r.value("description").toString());
            m_repos->addItem(it);
        }
        status(repos.isEmpty() ? "No repositories you can see yet." : QStringLiteral("%1 repositories").arg(repos.size()));
        // Restore #<owner>/<repo>[/path] from the page URL.
        QString hash = m_api->locationHash();
        const QString lines = splitLines(hash);
        const QStringList parts = hash.split('/', Qt::SkipEmptyParts);
        if (parts.size() >= 2) {
            const QString repo = parts[0] + "/" + parts[1];
            bool found = false;
            for (int i = 0; i < m_repos->count() && !found; ++i)
                if (m_repos->item(i)->data(PathRole).toString() == repo) {
                    found = true;
                    if (repo == m_repo) {  // already open (the list reloaded after sign-in): just reselect
                        const QSignalBlocker block(m_repos);
                        m_repos->setCurrentRow(i);
                        break;
                    }
                    m_pendingPath = parts.mid(2).join('/');
                    m_pendingLines = lines;
                    m_repos->setCurrentRow(i);
                }
            // An old link (from before repos moved under their owner, or before a rename): the server
            // resolves the old name, and openRepo switches to the current one.
            if (!found && repo != m_repo) {
                m_pendingPath = parts.mid(2).join('/');
                m_pendingLines = lines;
                openRepo(repo);
            }
        }
    });
}

void ForgeWindow::openRepo(const QString &repo)
{
    m_repo = repo;
    m_main->setCurrentIndex(1);
    m_title->setText(repo);
    m_desc->clear();
    m_desc->setToolTip(QString());
    m_crumbs->setText(QStringLiteral("<b>%1</b>").arg(repo.section('/', 1).toHtmlEscaped()));
    m_clone->clear();
    m_ref->clear();
    m_entries->clear();
    m_commits->clear();
    m_tabs->setTabText(1, "Commits");
    m_info = {};
    m_settings->setEnabled(false);
    status("Loading " + repo + "…");
    m_api->get(repoPath(repo), {}, this, [this, repo](const QJsonObject &info, const QString &error) {
        if (repo != m_repo)
            return;
        if (!error.isEmpty())
            return showRepoError(repo, info.value(Api::StatusKey).toInt(), error);
        const QString current = info.value("repo").toString();
        if (!current.isEmpty() && current != repo) {  // opened by an old name
            m_repo = current;
            const QSignalBlocker block(m_repos);
            for (int i = 0; i < m_repos->count(); ++i)
                if (m_repos->item(i)->data(PathRole).toString() == current)
                    m_repos->setCurrentRow(i);
        }
        m_info = info;
        fillSettings(info);
        const QString desc = info.value("description").toString();
        m_desc->setText(desc);
        m_desc->setToolTip(desc);
        m_title->setText(m_repo + (info.value("public").toBool() ? "" : "  (private)"));
        m_clone->setText("git clone " + info.value("clone_url").toString());
        const QString def = info.value("default").toString();
        const QJsonObject branches = info.value("branches").toObject();
        const QJsonObject tags = info.value("tags").toObject();
        if (!def.isEmpty() && branches.contains(def))
            m_ref->addItem(def);
        for (const QString &b : branches.keys())
            if (b != def)
                m_ref->addItem(b);
        for (const QString &t : tags.keys())
            m_ref->addItem(t);
        if (branches.isEmpty()) {
            m_filesStack->setCurrentIndex(0);
            m_crumbs->setText("<i>Nothing has been pushed yet.</i>");
            return status("Empty repository");
        }
        const QString path = m_pendingPath;
        m_pendingPath.clear();
        browse(path);
        loadLog();
    });
}

void ForgeWindow::setBreadcrumb(const QString &path, bool isFile)
{
    const QStringList parts = path.split('/', Qt::SkipEmptyParts);
    QString html = QStringLiteral("<a href=\"/\">%1</a>").arg(m_repo.section('/', 1).toHtmlEscaped());
    QString acc;
    for (int i = 0; i < parts.size(); ++i) {
        acc += (acc.isEmpty() ? "" : "/") + parts[i];
        const bool last = i == parts.size() - 1;
        html += " / ";
        html += last ? "<b>" + parts[i].toHtmlEscaped() + "</b>"
                     : QStringLiteral("<a href=\"/%1\">%2</a>").arg(acc.toHtmlEscaped(), parts[i].toHtmlEscaped());
    }
    if (isFile)
        html += QStringLiteral("  ·  <a href=\"/%1\">back</a>").arg(parts.mid(0, parts.size() - 1).join('/').toHtmlEscaped());
    m_crumbs->setText(html);
}

void ForgeWindow::browse(const QString &path)
{
    const QString repo = m_repo;
    QUrlQuery q;
    q.addQueryItem("ref", ref());
    q.addQueryItem("path", path);
    status("Loading…");
    m_api->get(repoPath(repo) + "/tree", q, this, [this, repo](const QJsonObject &body, const QString &error) {
        if (repo != m_repo)
            return;
        if (!error.isEmpty())
            return status(error, true);
        m_path = body.value("path").toString();
        const bool isFile = body.value("type").toString() == "file";
        setBreadcrumb(m_path, isFile);
        m_viewer->setMarkedLines(0, 0, false);  // line marks belong to the file they were made in
        if (!isFile)
            m_pendingLines.clear();
        updateHash();
        status(QStringLiteral("%1 @ %2").arg(ref(), body.value("commit").toString().left(12)));
        if (isFile)
            return showFile(body);
        m_entries->clear();
        for (const auto &v : body.value("entries").toArray()) {
            const QJsonObject e = v.toObject();
            const QString name = e.value("name").toString();
            const bool dir = name.endsWith('/');
            auto *it = new QTreeWidgetItem(m_entries);
            it->setText(0, name);
            it->setIcon(0, style()->standardIcon(dir ? QStyle::SP_DirIcon : QStyle::SP_FileIcon));
            if (!e.value("size").isNull())
                it->setText(1, humanSize(e.value("size").toInteger()));
            it->setTextAlignment(1, Qt::AlignRight | Qt::AlignVCenter);
            const QString clean = dir ? name.chopped(1) : name;
            it->setData(0, PathRole, m_path.isEmpty() ? clean : m_path + "/" + clean);
            it->setData(0, DirRole, dir);
        }
        m_filesStack->setCurrentIndex(0);
    });
}

void ForgeWindow::syncHighlightTheme()
{
    // Highlight colours follow the viewer: dark-on-light or light-on-dark (the browser's colour scheme).
    m_highlighter->setDark(m_viewer->palette().color(QPalette::Base).lightness() < 128);
}

void ForgeWindow::changeEvent(QEvent *event)
{
    QMainWindow::changeEvent(event);
    if (event->type() == QEvent::PaletteChange)
        syncHighlightTheme();
}

void ForgeWindow::showFile(const QJsonObject &file)
{
    QString lang;
    if (file.value("binary").toBool()) {
        m_highlighter->setFileName(QString());
        m_viewer->setPlainText(QStringLiteral("(binary file, %1)").arg(humanSize(file.value("size").toInteger())));
    } else {
        const QString content = file.value("content").toString();
        // Huge files stay plain: highlighting runs on the UI thread and would freeze the page.
        constexpr qsizetype HighlightLimit = 1 << 20;
        m_highlighter->setFileName(QString());
        m_viewer->setPlainText(content);
        if (content.size() <= HighlightLimit)
            lang = m_highlighter->setFileName(file.value("path").toString());
    }
    if (!lang.isEmpty())
        m_status->setText(m_status->text() + " · " + lang);
    m_filesStack->setCurrentIndex(1);
    // Line link from the URL (#L12 or #L12-L20): mark and scroll to it once the view has its size.
    const QString lines = std::exchange(m_pendingLines, QString());
    m_viewer->setMarkedLines(0, 0, false);
    if (!lines.isEmpty()) {
        const QStringList ends = lines.split('-');
        const int first = ends.value(0).mid(1).toInt();
        const QString lastStr = ends.value(1);
        const int last = lastStr.isEmpty() ? first : lastStr.mid(lastStr.startsWith('L') ? 1 : 0).toInt();
        QTimer::singleShot(0, this, [this, first, last] {
            m_viewer->setMarkedLines(first, last, true);
            updateHash();
        });
    }
    m_filesStack->setCurrentIndex(1);
}

void ForgeWindow::loadLog()
{
    const QString repo = m_repo;
    QUrlQuery q;
    q.addQueryItem("ref", ref());
    q.addQueryItem("limit", "100");
    m_tabs->setTabText(1, "Commits");
    m_api->get(repoPath(repo) + "/commits", q, this, [this, repo](const QJsonObject &body, const QString &error) {
        if (repo != m_repo)
            return;
        m_commits->clear();
        if (!error.isEmpty()) {
            qWarning("forge: loading commits for %s failed: %s", qPrintable(repo), qPrintable(error));
            auto *it = new QTreeWidgetItem(m_commits);
            it->setText(0, "Could not load commits: " + error + " (open this tab again to retry)");
            m_tabs->setTabText(1, "Commits (error)");
            return;
        }
        const QJsonArray commits = body.value("commits").toArray();
        if (commits.isEmpty())
            (new QTreeWidgetItem(m_commits))->setText(0, "No commits on " + ref());
        for (const auto &v : commits) {
            const QJsonObject c = v.toObject();
            const QString msg = c.value("message").toString();
            QString author = c.value("author").toString();
            author = author.section(" <", 0, 0);
            const QDateTime when = QDateTime::fromString(c.value("date").toString(), Qt::ISODate);
            auto *it = new QTreeWidgetItem(m_commits);
            it->setText(0, msg.section('\n', 0, 0));
            it->setToolTip(0, msg);
            it->setText(1, author);
            it->setText(2, QLocale().toString(when.toLocalTime(), QLocale::ShortFormat));
            it->setText(3, c.value("sha").toString().left(10));
            it->setFont(3, QFontDatabase::systemFont(QFontDatabase::FixedFont));
            it->setData(0, ShaRole, c.value("sha").toString());
        }
        m_tabs->setTabText(1, QStringLiteral("Commits (%1)").arg(commits.size()));
    });
}

void ForgeWindow::updateHash()
{
    QString hash = m_repo + (m_path.isEmpty() ? "" : "/" + m_path);
    if (m_filesStack->currentIndex() == 1 && m_viewer->markedFirst() > 0) {
        hash += QStringLiteral("#L%1").arg(m_viewer->markedFirst());
        if (m_viewer->markedLast() > m_viewer->markedFirst())
            hash += QStringLiteral("-L%1").arg(m_viewer->markedLast());
    }
    m_api->setLocationHash(hash);
}

QWidget *ForgeWindow::buildSettings()
{
    auto *page = new QWidget;
    auto *lay = new QVBoxLayout(page);
    lay->setContentsMargins(8, 8, 8, 8);

    auto *general = new QGroupBox("General");
    auto *form = new QFormLayout(general);
    // Repos always belong to their owner (the signed-in handle); only the name part can change.
    auto *nameRow = new QHBoxLayout;
    m_ownerPrefix = new QLabel;
    nameRow->addWidget(m_ownerPrefix);
    m_editName = new QLineEdit;
    m_editName->setPlaceholderText("repo-name");
    m_editName->setToolTip("Renaming changes the clone URL; the old URL keeps working as an alias.");
    nameRow->addWidget(m_editName, 1);
    form->addRow("Name:", nameRow);
    m_editDesc = new QLineEdit;
    m_editDesc->setPlaceholderText("A short description");
    form->addRow("Description:", m_editDesc);
    m_defaultBranch = new QComboBox;
    m_defaultBranch->setMinimumWidth(160);
    form->addRow("Default branch:", m_defaultBranch);
    m_public = new QCheckBox("Public: anyone can browse and clone it without signing in");
    form->addRow("Visibility:", m_public);
    m_save = new QPushButton("Save changes");
    m_save->setIcon(style()->standardIcon(QStyle::SP_DialogSaveButton));
    auto *saveRow = new QHBoxLayout;
    saveRow->addStretch(1);
    saveRow->addWidget(m_save);
    form->addRow(saveRow);
    lay->addWidget(general);
    connect(m_save, &QPushButton::clicked, this, &ForgeWindow::saveSettings);
    connect(m_editName, &QLineEdit::returnPressed, this, &ForgeWindow::saveSettings);
    connect(m_editDesc, &QLineEdit::returnPressed, this, &ForgeWindow::saveSettings);

    // No modal dialogs: they need a nested event loop, which Qt for WebAssembly can't run.
    auto *danger = new QGroupBox("Delete repository");
    danger->setStyleSheet("QGroupBox { color: #c62828; }");
    auto *dl = new QVBoxLayout(danger);
    auto *warn = new QLabel("This deletes the repository and all of its history. It can't be undone. "
                            "Type the repository's name to confirm.");
    warn->setWordWrap(true);
    dl->addWidget(warn);
    auto *delRow = new QHBoxLayout;
    m_confirmDelete = new QLineEdit;
    delRow->addWidget(m_confirmDelete, 1);
    m_delete = new QPushButton("Delete");
    m_delete->setIcon(style()->standardIcon(QStyle::SP_TrashIcon));
    m_delete->setEnabled(false);
    delRow->addWidget(m_delete);
    dl->addLayout(delRow);
    lay->addWidget(danger);
    lay->addStretch(1);
    connect(m_confirmDelete, &QLineEdit::textChanged, this, [this](const QString &t) {
        m_delete->setEnabled(!m_repo.isEmpty() && t.trimmed() == m_repo.section('/', 1));
    });
    connect(m_delete, &QPushButton::clicked, this, &ForgeWindow::deleteRepo);
    page->setEnabled(false);
    return page;
}

void ForgeWindow::fillSettings(const QJsonObject &info)
{
    m_ownerPrefix->setText(info.value("project").toString() + " /");
    m_editName->setText(info.value("name").toString());
    m_editDesc->setText(info.value("description").toString());
    m_public->setChecked(info.value("public").toBool());
    m_defaultBranch->clear();
    const QString def = info.value("default").toString();
    const QStringList branches = info.value("branches").toObject().keys();
    for (const QString &b : branches)
        m_defaultBranch->addItem(b);
    if (!def.isEmpty() && !branches.contains(def))
        m_defaultBranch->addItem(def);  // empty repo: HEAD names the branch the first push will create
    m_defaultBranch->setCurrentText(def);
    m_defaultBranch->setEnabled(m_defaultBranch->count() > 1);
    m_confirmDelete->clear();
    m_confirmDelete->setPlaceholderText(info.value("name").toString());
    m_settings->setEnabled(true);
}

void ForgeWindow::saveSettings()
{
    if (m_repo.isEmpty() || m_info.isEmpty())
        return;
    QJsonObject body;
    const QString name = m_editName->text().trimmed();
    if (!name.isEmpty() && name != m_info.value("name").toString())
        body["name"] = name;
    if (m_editDesc->text().trimmed() != m_info.value("description").toString())
        body["description"] = m_editDesc->text().trimmed();
    if (m_public->isChecked() != m_info.value("public").toBool())
        body["public"] = m_public->isChecked();
    const QString branch = m_defaultBranch->currentText();
    if (!branch.isEmpty() && branch != m_info.value("default").toString())
        body["default_branch"] = branch;
    if (body.isEmpty())
        return status("Nothing to save.");
    const QString repo = m_repo;
    m_save->setEnabled(false);
    status("Saving settings…");
    m_api->send("PATCH", repoPath(repo), body, this, [this, repo](const QJsonObject &info, const QString &error) {
        m_save->setEnabled(true);
        if (!error.isEmpty())
            return status("Could not save settings: " + error, true);
        const QString now = info.value("repo").toString();
        m_pendingPath = m_path;
        m_api->setLocationHash(now + (m_path.isEmpty() ? "" : "/" + m_path));
        m_repo.clear();  // reopen (as the new name, if renamed) once the list reloads
        loadRepos();
        status("Settings saved.");
    });
}

void ForgeWindow::deleteRepo()
{
    const QString repo = m_repo;
    if (repo.isEmpty() || m_confirmDelete->text().trimmed() != repo.section('/', 1))
        return;
    m_delete->setEnabled(false);
    status("Deleting " + repo + "…");
    m_api->send("DELETE", repoPath(repo), {}, this, [this, repo](const QJsonObject &, const QString &error) {
        if (!error.isEmpty()) {
            m_delete->setEnabled(true);
            return status("Could not delete " + repo + ": " + error, true);
        }
        m_repo.clear();
        m_api->setLocationHash(QString());
        m_main->setCurrentIndex(0);
        m_tabs->setCurrentIndex(0);
        loadRepos();
        status("Deleted " + repo + ".");
    });
}

QString ForgeWindow::shortName(const QString &repo) const
{
    // Your own repos show by name alone; anyone else's keep their owner.
    return !m_owner.isEmpty() && repo.section('/', 0, 0) == m_owner ? repo.section('/', 1) : repo;
}

void ForgeWindow::createRepo()
{
    const QString name = m_newName->text().trimmed();
    if (name.isEmpty())
        return status("Type a name for the new repository.");
    QJsonObject body;
    body["name"] = name;
    m_create->setEnabled(false);
    status("Creating " + name + "…");
    m_api->send("POST", "repos", body, this, [this, name](const QJsonObject &info, const QString &error) {
        m_create->setEnabled(true);
        if (!error.isEmpty())
            return status("Could not create " + name + ": " + error, true);
        m_newName->clear();
        const QString repo = info.value("repo").toString();
        m_pendingPath.clear();
        m_api->setLocationHash(repo);
        m_repo.clear();
        loadRepos();  // selects it from the hash
        status("Created " + repo + ". Push to " + info.value("clone_url").toString());
    });
}
