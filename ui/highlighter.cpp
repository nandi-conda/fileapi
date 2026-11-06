#include "highlighter.h"

#include <QFileInfo>
#include <QHash>

namespace {
using RE = QRegularExpression;

QString words(const char *list)
{
    return QStringLiteral("\\b(?:") + QString::fromLatin1(list).simplified().replace(' ', '|') + QStringLiteral(")\\b");
}

// Language ids, chosen from the file name.
enum class Lang { None, C, Cpp, Python, JavaScript, TypeScript, Rust, Go, Java, Kotlin, CSharp, Swift, Shell, Json,
                  Yaml, Toml, Ini, Html, Xml, Css, Markdown, Sql, Make, Docker, CMake, Lua, Ruby, Php, Diff, Nix,
                  Zig, Haskell, Elixir, Dart, Qml };

const QHash<QString, Lang> &byExtension()
{
    static const QHash<QString, Lang> map = {
        {"c", Lang::C}, {"h", Lang::Cpp},
        {"cc", Lang::Cpp}, {"cpp", Lang::Cpp}, {"cxx", Lang::Cpp}, {"hpp", Lang::Cpp}, {"hh", Lang::Cpp},
        {"hxx", Lang::Cpp}, {"ino", Lang::Cpp}, {"m", Lang::C}, {"mm", Lang::Cpp},
        {"py", Lang::Python}, {"pyi", Lang::Python}, {"pyw", Lang::Python},
        {"js", Lang::JavaScript}, {"mjs", Lang::JavaScript}, {"cjs", Lang::JavaScript}, {"jsx", Lang::JavaScript},
        {"ts", Lang::TypeScript}, {"tsx", Lang::TypeScript}, {"mts", Lang::TypeScript}, {"cts", Lang::TypeScript},
        {"rs", Lang::Rust}, {"go", Lang::Go}, {"java", Lang::Java}, {"kt", Lang::Kotlin}, {"kts", Lang::Kotlin},
        {"cs", Lang::CSharp}, {"swift", Lang::Swift},
        {"sh", Lang::Shell}, {"bash", Lang::Shell}, {"zsh", Lang::Shell}, {"fish", Lang::Shell},
        {"ksh", Lang::Shell}, {"env", Lang::Shell}, {"ps1", Lang::Shell},
        {"json", Lang::Json}, {"jsonc", Lang::Json}, {"json5", Lang::Json}, {"jsonl", Lang::Json},
        {"ndjson", Lang::Json}, {"lock", Lang::Toml}, {"webmanifest", Lang::Json}, {"ipynb", Lang::Json},
        {"yaml", Lang::Yaml}, {"yml", Lang::Yaml}, {"toml", Lang::Toml},
        {"ini", Lang::Ini}, {"cfg", Lang::Ini}, {"conf", Lang::Ini}, {"properties", Lang::Ini},
        {"desktop", Lang::Ini}, {"service", Lang::Ini}, {"editorconfig", Lang::Ini},
        {"html", Lang::Html}, {"htm", Lang::Html}, {"vue", Lang::Html}, {"svelte", Lang::Html},
        {"xml", Lang::Xml}, {"svg", Lang::Xml}, {"plist", Lang::Xml}, {"ui", Lang::Xml}, {"qrc", Lang::Xml},
        {"xsd", Lang::Xml}, {"xsl", Lang::Xml}, {"csproj", Lang::Xml},
        {"css", Lang::Css}, {"scss", Lang::Css}, {"sass", Lang::Css}, {"less", Lang::Css},
        {"md", Lang::Markdown}, {"markdown", Lang::Markdown}, {"mdx", Lang::Markdown},
        {"sql", Lang::Sql}, {"mk", Lang::Make}, {"mak", Lang::Make}, {"just", Lang::Make},
        {"dockerfile", Lang::Docker}, {"containerfile", Lang::Docker}, {"cmake", Lang::CMake},
        {"lua", Lang::Lua}, {"rb", Lang::Ruby}, {"rake", Lang::Ruby}, {"gemspec", Lang::Ruby},
        {"php", Lang::Php}, {"diff", Lang::Diff}, {"patch", Lang::Diff}, {"nix", Lang::Nix},
        {"zig", Lang::Zig}, {"hs", Lang::Haskell}, {"ex", Lang::Elixir}, {"exs", Lang::Elixir},
        {"dart", Lang::Dart}, {"qml", Lang::Qml},
        {"gitignore", Lang::Shell}, {"gitattributes", Lang::Shell}, {"dockerignore", Lang::Shell},
        {"fastapicloudignore", Lang::Shell}, {"bashrc", Lang::Shell}, {"zshrc", Lang::Shell}, {"profile", Lang::Shell},
    };
    return map;
}

Lang detect(const QString &fileName)
{
    const QString name = QFileInfo(fileName).fileName();
    const QString lower = name.toLower();
    static const QHash<QString, Lang> byName = {
        {"makefile", Lang::Make}, {"gnumakefile", Lang::Make}, {"justfile", Lang::Make},
        {"dockerfile", Lang::Docker}, {"containerfile", Lang::Docker}, {"cmakelists.txt", Lang::CMake},
        {"gemfile", Lang::Ruby}, {"rakefile", Lang::Ruby}, {"vagrantfile", Lang::Ruby},
        {"pkgbuild", Lang::Shell}, {"apkbuild", Lang::Shell}, {"cargo.lock", Lang::Toml},
        {"pixi.lock", Lang::Yaml}, {"uv.lock", Lang::Toml}, {"poetry.lock", Lang::Toml},
        {"package-lock.json", Lang::Json}, {"yarn.lock", Lang::Yaml}, {"flake.lock", Lang::Json},
    };
    if (auto it = byName.constFind(lower); it != byName.constEnd())
        return *it;
    if (lower.startsWith("dockerfile.") || lower.endsWith(".dockerfile"))
        return Lang::Docker;
    if (lower.startsWith(".env"))
        return Lang::Shell;
    const int dot = lower.lastIndexOf('.');
    if (dot < 0)
        return Lang::None;
    return byExtension().value(lower.mid(dot + 1), Lang::None);
}

const char *langName(Lang l)
{
    switch (l) {
    case Lang::None: return "";
    case Lang::C: return "C";
    case Lang::Cpp: return "C++";
    case Lang::Python: return "Python";
    case Lang::JavaScript: return "JavaScript";
    case Lang::TypeScript: return "TypeScript";
    case Lang::Rust: return "Rust";
    case Lang::Go: return "Go";
    case Lang::Java: return "Java";
    case Lang::Kotlin: return "Kotlin";
    case Lang::CSharp: return "C#";
    case Lang::Swift: return "Swift";
    case Lang::Shell: return "Shell";
    case Lang::Json: return "JSON";
    case Lang::Yaml: return "YAML";
    case Lang::Toml: return "TOML";
    case Lang::Ini: return "INI";
    case Lang::Html: return "HTML";
    case Lang::Xml: return "XML";
    case Lang::Css: return "CSS";
    case Lang::Markdown: return "Markdown";
    case Lang::Sql: return "SQL";
    case Lang::Make: return "Makefile";
    case Lang::Docker: return "Dockerfile";
    case Lang::CMake: return "CMake";
    case Lang::Lua: return "Lua";
    case Lang::Ruby: return "Ruby";
    case Lang::Php: return "PHP";
    case Lang::Diff: return "Diff";
    case Lang::Nix: return "Nix";
    case Lang::Zig: return "Zig";
    case Lang::Haskell: return "Haskell";
    case Lang::Elixir: return "Elixir";
    case Lang::Dart: return "Dart";
    case Lang::Qml: return "QML";
    }
    return "";
}

// Common token patterns.
const char *const kDq = R"("(?:[^"\\]|\\.)*")";
const char *const kSq = R"('(?:[^'\\]|\\.)*')";
const char *const kBt = R"(`(?:[^`\\]|\\.)*`)";
const char *const kNumber = R"(\b(?:0[xX][0-9a-fA-F_]+|0[bB][01_]+|0[oO][0-7_]+|\d[\d_]*(?:\.\d[\d_]*)?(?:[eE][+-]?\d+)?)[a-zA-Z]*\b)";
const char *const kCall = R"(\b([A-Za-z_]\w*)\s*(?=\())";
}  // namespace

CodeHighlighter::CodeHighlighter(QTextDocument *doc) : QSyntaxHighlighter(doc)
{
    buildFormats();
}

void CodeHighlighter::setDark(bool dark)
{
    if (dark == m_dark)
        return;
    m_dark = dark;
    buildFormats();
    rehighlight();
}

void CodeHighlighter::buildFormats()
{
    // GitHub-like palettes; dark ones are tuned for a #1e1e1e-ish background.
    struct C { const char *light, *dark; bool italic, bold; };
    static const C colors[KindCount] = {
        /* Keyword  */ {"#cf222e", "#ff7b72", false, false},
        /* Type     */ {"#953800", "#ffa657", false, false},
        /* Constant */ {"#0550ae", "#79c0ff", false, false},
        /* String   */ {"#0a3069", "#a5d6ff", false, false},
        /* Comment  */ {"#6e7781", "#8b949e", true, false},
        /* Number   */ {"#0550ae", "#79c0ff", false, false},
        /* Function */ {"#8250df", "#d2a8ff", false, false},
        /* Meta     */ {"#8250df", "#d2a8ff", false, false},
        /* Tag      */ {"#116329", "#7ee787", false, false},
        /* Attr     */ {"#0550ae", "#79c0ff", false, false},
        /* Heading  */ {"#0550ae", "#79c0ff", false, true},
        /* Added    */ {"#116329", "#7ee787", false, false},
        /* Removed  */ {"#82071e", "#ffa198", false, false},
    };
    for (int k = 0; k < KindCount; ++k) {
        QTextCharFormat f;
        f.setForeground(QColor(QString::fromLatin1(m_dark ? colors[k].dark : colors[k].light)));
        f.setFontItalic(colors[k].italic);
        if (colors[k].bold)
            f.setFontWeight(QFont::Bold);
        m_formats[k] = f;
    }
}

QString CodeHighlighter::setFileName(const QString &fileName)
{
    const Lang lang = detect(fileName);
    m_rules.clear();
    m_spans.clear();

    auto rule = [this](const QString &pattern, Kind kind, int group = 0,
                       RE::PatternOptions opts = RE::NoPatternOption) {
        m_rules.append({RE(pattern, opts), kind, group});
    };
    auto span = [this](const QString &start, const QString &end, Kind kind) {
        m_spans.append({RE(start), RE(end), kind});
    };
    auto cStyle = [&](const char *keywords, const char *types, const char *constants) {
        span(R"(/\*)", R"(\*/)", Comment);
        rule(R"(//.*)", Comment);
        rule(kDq, String);
        rule(kSq, String);
        rule(words(keywords), Keyword);
        if (types && *types)
            rule(words(types), Type);
        rule(words(constants), Constant);
        rule(kNumber, Number);
        rule(R"(\b[A-Z][A-Za-z0-9_]*[a-z][A-Za-z0-9_]*\b)", Type);  // CamelCase names: classes, types
        rule(kCall, Function, 1);
    };

    switch (lang) {
    case Lang::None:
        break;
    case Lang::C:
    case Lang::Cpp:
        rule(R"(^\s*#\s*\w+)", Meta);
        rule(R"((?<=#include)\s*<[^>]*>)", String);
        cStyle(lang == Lang::C
                   ? "auto break case const continue default do else enum extern for goto if inline register "
                     "restrict return sizeof static struct switch typedef union volatile while"
                   : "alignas alignof auto break case catch class const consteval constexpr constinit const_cast "
                     "continue co_await co_return co_yield decltype default delete do dynamic_cast else enum "
                     "explicit export extern final for friend goto if inline mutable namespace new noexcept "
                     "operator override private protected public reinterpret_cast requires return sizeof static "
                     "static_assert static_cast struct switch template this thread_local throw try typedef "
                     "typeid typename union using virtual volatile while emit signals slots Q_OBJECT",
               "bool char char8_t char16_t char32_t double float int long short signed unsigned void wchar_t "
               "size_t ssize_t ptrdiff_t int8_t int16_t int32_t int64_t uint8_t uint16_t uint32_t uint64_t "
               "intptr_t uintptr_t qint64 quint64 qreal",
               "true false nullptr NULL");
        break;
    case Lang::JavaScript:
    case Lang::TypeScript:
    case Lang::Qml:
        span(R"(/\*)", R"(\*/)", Comment);
        rule(R"(//.*)", Comment);
        rule(kDq, String);
        rule(kSq, String);
        rule(kBt, String);
        rule(words("as async await break case catch class const continue debugger default delete do else "
                   "export extends finally for from function get if import in instanceof let new of return "
                   "set static super switch this throw try typeof var void while with yield abstract declare "
                   "enum implements interface keyof namespace private protected public readonly satisfies "
                   "type property signal readonly required component pragma"),
             Keyword);
        rule(words("string number boolean any unknown never object symbol bigint void"), Type);
        rule(words("true false null undefined NaN Infinity"), Constant);
        rule(R"(@\w+)", Meta);
        rule(kNumber, Number);
        rule(R"(\b[A-Z][A-Za-z0-9_]*[a-z][A-Za-z0-9_]*\b)", Type);
        rule(kCall, Function, 1);
        break;
    case Lang::Java:
    case Lang::Kotlin:
    case Lang::CSharp:
    case Lang::Swift:
    case Lang::Dart:
        rule(R"(@\w+|^\s*#\s*\w+)", Meta);
        cStyle("abstract as async await base break case catch class const continue default defer delegate do "
               "else enum event explicit extends extension external final finally fn for foreach fun func "
               "get guard if implements implicit import in init interface internal is let lock namespace new "
               "object open operator out override package private protected public readonly record ref "
               "required return sealed set static struct super switch synchronized this throw throws try "
               "typealias typeof using val var virtual void volatile when where while with yield",
               "boolean byte char double float int long short string bool decimal object Int Double Float "
               "String Bool Unit Any dynamic",
               "true false null nil");
        break;
    case Lang::Rust:
        rule(R"(#!?\[[^\]]*\])", Meta);
        rule(R"(\b\w+!)", Function);  // macros
        rule(R"('\w+\b(?!'))", Meta);   // lifetimes
        cStyle("as async await break const continue crate dyn else enum extern fn for if impl in let loop "
               "match mod move mut pub ref return self Self static struct super trait type unsafe use where "
               "while",
               "bool char str i8 i16 i32 i64 i128 isize u8 u16 u32 u64 u128 usize f32 f64",
               "true false None Some Ok Err");
        break;
    case Lang::Go:
        rule(kBt, String);
        cStyle("break case chan const continue default defer else fallthrough for func go goto if import "
               "interface map package range return select struct switch type var",
               "bool byte complex64 complex128 error float32 float64 int int8 int16 int32 int64 rune string "
               "uint uint8 uint16 uint32 uint64 uintptr any",
               "true false nil iota");
        break;
    case Lang::Zig:
        cStyle("addrspace align allowzero and anyframe anytype asm async await break callconv catch comptime "
               "const continue defer else enum errdefer error export extern fn for if inline linksection "
               "noalias noinline nosuspend or orelse packed pub resume return struct suspend switch test "
               "threadlocal try union unreachable usingnamespace var volatile while",
               "bool f16 f32 f64 i8 i16 i32 i64 isize u8 u16 u32 u64 usize void type anyerror",
               "true false null undefined");
        rule(R"(@\w+)", Function);
        break;
    case Lang::Python:
        span(R"((?:\b[rRbBuUfF]{1,2})?""")", R"(""")", String);
        span(R"((?:\b[rRbBuUfF]{1,2})?''')", R"(''')", String);
        rule(R"(#.*)", Comment);
        rule(QStringLiteral(R"((?:\b[rRbBuUfF]{1,2})?)") + kDq, String);
        rule(QStringLiteral(R"((?:\b[rRbBuUfF]{1,2})?)") + kSq, String);
        rule(R"(^\s*@[\w.]+)", Meta);
        rule(words("and as assert async await break class continue def del elif else except finally for "
                   "from global if import in is lambda match case nonlocal not or pass raise return try "
                   "while with yield"),
             Keyword);
        rule(words("True False None self cls"), Constant);
        rule(words("int float str bytes bool list dict set tuple object type"), Type);
        rule(kNumber, Number);
        rule(R"(\b[A-Z][A-Za-z0-9_]*[a-z][A-Za-z0-9_]*\b)", Type);
        rule(kCall, Function, 1);
        break;
    case Lang::Ruby:
    case Lang::Elixir:
        rule(R"(#.*)", Comment);
        rule(kDq, String);
        rule(kSq, String);
        rule(R"((?<![:\w]):\w+)", Constant);  // symbols / atoms
        rule(words("alias and begin break case catch class def defmodule defp defstruct defmacro do else "
                   "elsif end ensure fn for if import in module next nil not or raise receive redo require "
                   "rescue retry return self super then unless until use when while yield"),
             Keyword);
        rule(words("true false nil"), Constant);
        rule(kNumber, Number);
        rule(R"(\b[A-Z]\w*)", Type);
        break;
    case Lang::Lua:
        span(R"(--\[\[)", R"(\]\])", Comment);
        rule(R"(--.*)", Comment);
        rule(kDq, String);
        rule(kSq, String);
        rule(words("and break do else elseif end for function goto if in local not or repeat return then "
                   "until while"),
             Keyword);
        rule(words("true false nil self"), Constant);
        rule(kNumber, Number);
        rule(kCall, Function, 1);
        break;
    case Lang::Php:
        span(R"(/\*)", R"(\*/)", Comment);
        rule(R"((?://|#).*)", Comment);
        rule(kDq, String);
        rule(kSq, String);
        rule(R"(\$\w+)", Attr);
        rule(R"(<\?php|\?>)", Meta);
        rule(words("abstract and array as break case catch class clone const continue declare default do echo "
                   "else elseif enum extends final finally fn for foreach function global if implements "
                   "include interface match namespace new or private protected public readonly require "
                   "return static switch throw trait try use var while yield"),
             Keyword);
        rule(words("true false null TRUE FALSE NULL"), Constant);
        rule(kNumber, Number);
        rule(kCall, Function, 1);
        break;
    case Lang::Haskell:
        span(R"(\{-)", R"(-\})", Comment);
        rule(R"(--.*)", Comment);
        rule(kDq, String);
        rule(words("case class data default deriving do else foreign if import in infix infixl infixr instance "
                   "let module newtype of then type where"),
             Keyword);
        rule(R"(\b[A-Z]\w*)", Type);
        rule(kNumber, Number);
        break;
    case Lang::Shell:
    case Lang::Make:
    case Lang::Docker:
        rule(R"((?<![\w$])#.*)", Comment);
        rule(kDq, String);
        rule(kSq, String);
        rule(kBt, String);
        rule(R"(\$\{[^}]*\}|\$\(\(?[^)]*\)?\)|\$[\w@*#?$!-]|\{\{[^}]*\}\})", Attr);
        if (lang == Lang::Make) {
            rule(R"(^[\w.\-/ ]+(?=:(?!=)))", Function);       // targets / recipes
            rule(R"(^\s*@)", Meta);
            rule(words("set export import mod alias if else endif ifeq ifneq ifdef ifndef include define endef"),
                 Keyword);
        } else if (lang == Lang::Docker) {
            rule(words("FROM AS RUN CMD LABEL MAINTAINER EXPOSE ENV ADD COPY ENTRYPOINT VOLUME USER WORKDIR ARG "
                       "ONBUILD STOPSIGNAL HEALTHCHECK SHELL"),
                 Keyword, 0, RE::CaseInsensitiveOption);
        }
        rule(words("if then else elif fi case esac for while until do done in function return local export "
                   "readonly declare set unset shift exit source alias select time"),
             Keyword);
        rule(words("echo printf cd pwd test read eval exec trap mkdir rm cp mv ls cat grep sed awk curl git "
                   "sudo"),
             Function);
        rule(R"((?<=\s)--?[\w-]+)", Constant);
        rule(R"(\b\d+\b)", Number);
        break;
    case Lang::CMake:
        span(R"(#\[\[)", R"(\]\])", Comment);
        rule(R"(#.*)", Comment);
        rule(kDq, String);
        rule(R"(\$\{[^}]*\})", Attr);
        rule(R"(\b([A-Za-z_]\w*)\s*(?=\())", Function, 1);
        rule(R"(\b[A-Z][A-Z0-9_]{2,}\b)", Constant);
        break;
    case Lang::Nix:
        span(R"(/\*)", R"(\*/)", Comment);
        rule(R"(#.*)", Comment);
        rule(kDq, String);
        span(R"('')", R"('')", String);
        rule(words("assert else if in inherit let or rec then with import"), Keyword);
        rule(words("true false null"), Constant);
        rule(R"(\b[\w-]+(?=\s*=))", Attr);
        rule(kNumber, Number);
        break;
    case Lang::Sql:
        span(R"(/\*)", R"(\*/)", Comment);
        rule(R"(--.*)", Comment);
        rule(kSq, String);
        rule(kDq, Attr);
        rule(words("add all alter and any as asc begin between by case cascade check column commit constraint "
                   "create cross database default delete desc distinct drop else end exists foreign from full "
                   "group having if in index inner insert into is join key left like limit not null offset on "
                   "or order outer primary references replace returning right rollback select set table then "
                   "transaction trigger truncate union unique update using values view when where with conflict "
                   "do nothing"),
             Keyword, 0, RE::CaseInsensitiveOption);
        rule(words("int integer bigint smallint serial bigserial text varchar char boolean bool date time "
                   "timestamp timestamptz bytea json jsonb uuid real float double numeric decimal blob"),
             Type, 0, RE::CaseInsensitiveOption);
        rule(words("true false"), Constant, 0, RE::CaseInsensitiveOption);
        rule(kNumber, Number);
        rule(kCall, Function, 1);
        break;
    case Lang::Json:
        span(R"(/\*)", R"(\*/)", Comment);
        rule(R"(//.*)", Comment);
        rule(QStringLiteral("(%1)\\s*(?=:)").arg(QString::fromLatin1(kDq)), Attr, 1);
        rule(kDq, String);
        rule(words("true false null"), Constant);
        rule(R"(-?\b\d+(?:\.\d+)?(?:[eE][+-]?\d+)?\b)", Number);
        break;
    case Lang::Yaml:
        rule(R"((?<![^\s])#.*)", Comment);
        rule(R"(^(\s*(?:-\s+)?)([^\s#:'"][^:#]*?|"[^"]*"|'[^']*')\s*(?=:(?:\s|$)))", Attr, 2);
        rule(kDq, String);
        rule(kSq, String);
        rule(R"(^(?:---|\.\.\.)\s*$)", Meta);
        rule(R"([&*][\w-]+|!\S+)", Meta);
        rule(words("true false yes no on off null"), Constant, 0, RE::CaseInsensitiveOption);
        rule(R"((?<=[\s:\[,-])-?\d+(?:\.\d+)?\b)", Number);
        break;
    case Lang::Toml:
    case Lang::Ini:
        rule(lang == Lang::Ini ? R"(^\s*[#;].*)" : R"((?<![^\s])#.*)", Comment);
        rule(R"(^\s*\[[^\]]*\]+)", Heading);
        rule(R"(^(\s*)([\w.\-"' ]+?)(?=\s*[=:]))", Attr, 2);
        span(R"(""")", R"(""")", String);
        rule(kDq, String);
        rule(kSq, String);
        rule(words("true false"), Constant);
        rule(R"((?<=[=\s\[,])[+-]?\d[\d_:.\-TZ+]*\b)", Number);
        break;
    case Lang::Html:
    case Lang::Xml:
        span(R"(<!--)", R"(-->)", Comment);
        span(R"(<!\[CDATA\[)", R"(\]\]>)", String);
        rule(R"(<[!?][^>]*>)", Meta);
        rule(R"(</?\s*([\w:.-]+))", Tag, 1);
        rule(R"(\b([\w:.-]+)(?=\s*=\s*["']))", Attr, 1);
        rule(kDq, String);
        rule(kSq, String);
        rule(R"(&\w+;|&#\w+;)", Constant);
        break;
    case Lang::Css:
        span(R"(/\*)", R"(\*/)", Comment);
        rule(R"(//.*)", Comment);
        rule(kDq, String);
        rule(kSq, String);
        rule(R"(@[\w-]+)", Keyword);
        rule(R"(\$[\w-]+|--[\w-]+)", Attr);
        rule(R"(\b([a-z-]+)\s*(?=:[^:]))", Attr, 1);
        rule(R"(#[0-9a-fA-F]{3,8}\b)", Number);
        rule(R"(-?\b\d+(?:\.\d+)?(?:px|em|rem|%|vh|vw|s|ms|deg|fr|ch|ex|pt)?)", Number);
        rule(R"([.#][\w-]+|:{1,2}[\w-]+)", Tag);
        rule(R"(!important)", Keyword);
        break;
    case Lang::Markdown:
        span(R"(^\s*(?:```|~~~))", R"(^\s*(?:```|~~~)\s*$)", String);
        rule(R"(^#{1,6}\s.*)", Heading);
        rule(R"(^\s*(?:[-*+]|\d+\.)\s)", Keyword);
        rule(R"(^\s*>.*)", Comment);
        rule(R"(`[^`]+`)", String);
        rule(R"(!?\[[^\]]*\]\([^)]*\))", Attr);
        rule(R"(\*\*[^*]+\*\*|__[^_]+__)", Type);
        rule(R"(<!--.*?-->)", Comment);
        break;
    case Lang::Diff:
        rule(R"(^(?:\+\+\+|---|diff |index ).*)", Heading);
        rule(R"(^@@.*)", Meta);
        rule(R"(^\+.*)", Added);
        rule(R"(^-.*)", Removed);
        break;
    }
    rehighlight();
    return QString::fromLatin1(langName(lang));
}

void CodeHighlighter::highlightBlock(const QString &text)
{
    if (m_rules.isEmpty() && m_spans.isEmpty())
        return;
    setCurrentBlockState(highlightLine(text, std::max(0, previousBlockState()),
                                       [this](int start, int length, const QTextCharFormat &f) { setFormat(start, length, f); }));
}

int CodeHighlighter::highlightLine(const QString &text, int prevState, const FormatFn &setFormat) const
{
    if (m_rules.isEmpty() && m_spans.isEmpty())
        return 0;
    const int len = int(text.size());
    int pos = 0;

    // Continue a construct left open by the previous line.
    if (const int prev = prevState; prev > 0 && prev <= m_spans.size()) {
        const Span &s = m_spans[prev - 1];
        const auto end = s.end.match(text);
        if (!end.hasMatch()) {
            setFormat(0, len, m_formats[s.kind]);
            return prev;
        }
        setFormat(0, int(end.capturedEnd()), m_formats[s.kind]);
        pos = int(end.capturedEnd());
    }

    // Leftmost match wins; on a tie, spans then rules in declaration order. Each rule's next match is
    // cached and only re-run once the scan has moved past it.
    QVector<QRegularExpressionMatch> spanNext(m_spans.size());
    QVector<QRegularExpressionMatch> ruleNext(m_rules.size());
    QVector<int> spanAt(m_spans.size(), -2), ruleAt(m_rules.size(), -2);  // -2 = not computed, -1 = none
    auto next = [&](const QRegularExpression &re, QRegularExpressionMatch &m, int &at) {
        if (at == -1 || (at >= pos && at != -2))
            return at;
        m = re.match(text, pos);
        at = m.hasMatch() ? int(m.capturedStart()) : -1;
        return at;
    };

    while (pos < len) {
        int best = len, bestSpan = -1, bestRule = -1;
        for (int i = 0; i < m_spans.size(); ++i) {
            const int at = next(m_spans[i].start, spanNext[i], spanAt[i]);
            if (at >= 0 && at < best) {
                best = at;
                bestSpan = i;
            }
        }
        for (int i = 0; i < m_rules.size(); ++i) {
            const int at = next(m_rules[i].re, ruleNext[i], ruleAt[i]);
            if (at >= 0 && at < best) {
                best = at;
                bestSpan = -1;
                bestRule = i;
            }
        }
        if (bestSpan >= 0) {
            const Span &s = m_spans[bestSpan];
            const int bodyFrom = int(spanNext[bestSpan].capturedEnd());
            const auto end = s.end.match(text, bodyFrom);
            if (!end.hasMatch()) {
                setFormat(best, len - best, m_formats[s.kind]);
                return bestSpan + 1;
            }
            setFormat(best, int(end.capturedEnd()) - best, m_formats[s.kind]);
            pos = std::max(int(end.capturedEnd()), best + 1);
        } else if (bestRule >= 0) {
            const Rule &r = m_rules[bestRule];
            const auto &m = ruleNext[bestRule];
            if (m.capturedLength(r.group) > 0)
                setFormat(int(m.capturedStart(r.group)), int(m.capturedLength(r.group)), m_formats[r.kind]);
            pos = std::max(int(m.capturedEnd()), best + 1);
        } else {
            break;
        }
    }
    return 0;
}
