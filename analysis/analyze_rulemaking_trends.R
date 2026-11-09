library(data.table)

args <- commandArgs(trailingOnly = TRUE)
opt <- function(flag, default) {
  i <- match(flag, args)
  if (is.na(i) || i == length(args)) default else args[i + 1L]
}
at_root <- dir.exists("data")
data_dir <- opt("--data", if (at_root) "data" else "../data")
out_dir <- opt("--out", if (at_root) "build" else "../build")
site_only <- "--site-only" %in% args
dir.create(out_dir, showWarnings = FALSE, recursive = TRUE)
chart_from <- as.integer(opt("--chart-from", 1994))
n_boot <- as.integer(opt("--boot", 200))

THIS_YEAR <- as.integer(format(Sys.Date(), "%Y"))
MIN_YEAR <- 1970L
THRESHOLDS <- c(1000L, 10000L, 100000L)
ERAS <- list(c(1994L, 2007L), c(2008L, 2012L), c(2013L, 2017L), c(2018L, 2022L), c(2023L, THIS_YEAR))
MIN_TAIL <- 30L
MAX_XMIN <- 40L

BLUE <- "#2a78d6"
ORANGE <- "#eb6834"
SURFACE <- "#fcfcfb"
INK <- "#0b0b0b"
INK2 <- "#52514e"
MUTED <- "#898781"
GRID <- "#e1e0d9"

write_table <- function(dt, name) fwrite(dt, file.path(out_dir, name), na = "")

round_table <- function(dt, digits = 4) {
  for (j in names(dt)) if (is.double(dt[[j]])) set(dt, j = j, value = round(dt[[j]], digits))
  dt
}

fmt_si <- function(x) {
  ifelse(x >= 1e6, paste0(signif(x / 1e6, 3), "M"),
         ifelse(x >= 1e3, paste0(x / 1e3, "k"), as.character(x)))
}

year_of <- function(x) as.integer(format(as.Date(x, format = "%Y-%m-%d"), "%Y"))
min_chr <- function(x) if (all(is.na(x))) NA_character_ else min(x, na.rm = TRUE)
max_chr <- function(x) if (all(is.na(x))) NA_character_ else max(x, na.rm = TRUE)

threshold_counts <- function(n) {
  out <- lapply(THRESHOLDS, function(t) sum(n >= t))
  names(out) <- paste0("dockets_ge_", THRESHOLDS)
  out
}

scan <- fread(file.path(data_dir, "_docket_year_scan.csv"), encoding = "UTF-8", na.strings = "",
              colClasses = list(character = c("docket_id", "first_posted", "last_posted", "first_received")))
dockets <- as.data.table(read.csv(file.path(data_dir, "_rulemaking_dockets.csv"), colClasses = "character",
                                  na.strings = "", fileEncoding = "UTF-8", check.names = FALSE))

read_topup <- function(name) {
  path <- file.path(data_dir, name)
  if (!file.exists(path)) return(NULL)
  fread(path, encoding = "UTF-8", na.strings = "",
        colClasses = list(character = c("docket_id", "first_posted", "last_posted", "first_received")))
}

topup_dockets_file <- file.path(data_dir, "_topup_dockets.csv")
bucket_topup <- read_topup("_topup_scan.csv")
api_topup <- read_topup("_api_topup_scan.csv")
topup_comments <- if (is.null(bucket_topup)) 0L else sum(bucket_topup$published_comments)
api_comments <- if (is.null(api_topup)) 0L else sum(api_topup$published_comments)
topup_dockets <- 0L
scan <- rbind(scan, bucket_topup, api_topup, use.names = TRUE)
open_dockets <- data.table(docket_id = character(), comment_end_date = character(), comments_received = integer())
if (file.exists(file.path(data_dir, "_open_dockets.csv"))) {
  listed <- fread(file.path(data_dir, "_open_dockets.csv"), encoding = "UTF-8", na.strings = "",
                  colClasses = list(character = c("docket_id", "comment_end_date")))
  if (!"comments_received" %in% names(listed)) listed[, comments_received := NA_integer_]
  open_dockets <- listed[, .(docket_id, comment_end_date, comments_received = as.integer(comments_received))]
}
if (file.exists(topup_dockets_file)) {
  extra <- as.data.table(read.csv(topup_dockets_file, colClasses = "character", na.strings = "",
                                  fileEncoding = "UTF-8", check.names = FALSE))
  topup_dockets <- nrow(extra)
  dockets <- unique(rbind(dockets, extra, use.names = TRUE), by = "docket_id")
}

is_pra <- grepl("information\\s+collection", dockets$docket_title, ignore.case = TRUE, perl = TRUE)
is_frdoc <- grepl("FRDOC", dockets$docket_id, ignore.case = TRUE)
raw_totals <- scan[, .(n = sum(published_comments)), by = docket_id]
raw_n <- function(ids) sum(raw_totals$n[raw_totals$docket_id %in% ids])
dockets_before <- nrow(dockets)
comments_before <- sum(raw_totals$n[raw_totals$docket_id %in% dockets$docket_id])
exclusions <- data.table(
  rule = c("title contains 'information collection' (Paperwork Reduction Act notices)",
           "docket ID contains FRDOC (bundled Federal Register notices)",
           "either rule"),
  dockets = c(sum(is_pra), sum(is_frdoc), sum(is_pra | is_frdoc)),
  comments = c(raw_n(dockets$docket_id[is_pra]), raw_n(dockets$docket_id[is_frdoc]),
               raw_n(dockets$docket_id[is_pra | is_frdoc])))
exclusions[, share_of_dockets := round(dockets / dockets_before, 5)]
exclusions[, share_of_comments := round(comments / comments_before, 5)]
dockets <- dockets[!(is_pra | is_frdoc)]
scan <- scan[docket_id %in% dockets$docket_id]
open_dockets <- open_dockets[docket_id %in% dockets$docket_id]

per_docket <- scan[, .(published_comments = sum(published_comments),
                       first_posted = min_chr(first_posted),
                       last_posted = max_chr(last_posted),
                       first_received = min_chr(first_received)), by = docket_id]
ranked <- merge(dockets, per_docket, by = "docket_id", all.x = TRUE, sort = FALSE)
ranked[is.na(published_comments), published_comments := 0L]
ranked <- merge(ranked, open_dockets, by = "docket_id", all.x = TRUE, sort = FALSE)

id_pattern <- "^.*-((?:19[7-9]|20[0-3])[0-9])-.*$"
id_match <- grepl(id_pattern, ranked$docket_id, perl = TRUE)
id_year <- rep(NA_integer_, nrow(ranked))
id_year[id_match] <- as.integer(sub(id_pattern, "\\1", ranked$docket_id[id_match], perl = TRUE))
id_ok <- !is.na(id_year) & id_year >= MIN_YEAR & id_year <= THIS_YEAR
fallback <- year_of(ranked$first_received)
fallback <- ifelse(is.na(fallback), year_of(ranked$first_posted), fallback)
ranked[, docket_year := as.integer(ifelse(id_ok, id_year, fallback))]
setorder(ranked, -published_comments, docket_id)
ranked[, rank := .I]
setcolorder(ranked, c("rank", "docket_id", "agency_code", "docket_title", "published_comments",
                      "first_posted", "last_posted", "first_received", "docket_year", "comment_end_date",
                      "comments_received"))

dated <- merge(scan[!is.na(posted_year), .(published_comments = sum(published_comments)),
                    by = .(docket_id, posted_year = as.integer(posted_year))],
               ranked[, .(docket_id, docket_year)], by = "docket_id", all.x = TRUE)
backfilled <- !is.na(dated$docket_year) & dated$posted_year < dated$docket_year
backfilled_comments <- sum(dated$published_comments[backfilled])
dated <- dated[!backfilled, .(docket_id, posted_year, published_comments)]
setorder(dated, posted_year, -published_comments, docket_id)
undated_comments <- sum(scan[is.na(posted_year), published_comments])

by_year <- dated[, {
  total <- sum(published_comments)
  top10 <- sum(head(published_comments, 10L))
  c(list(partial_year = posted_year == THIS_YEAR,
         published_comments = total,
         active_dockets = .N,
         mean_per_active_docket = round(total / .N, 1),
         median_per_active_docket = median(as.numeric(published_comments)),
         top_docket = docket_id[1L],
         top_docket_comments = published_comments[1L],
         top_docket_share = round(published_comments[1L] / total, 4),
         top10_comments = top10,
         top10_share = round(top10 / total, 4),
         all_other_comments = total - top10),
    threshold_counts(published_comments))
}, by = posted_year]

top_by_year <- dated[, head(.SD, 10L), by = posted_year]
top_by_year[, rank_in_year := seq_len(.N), by = posted_year]
top_by_year <- merge(top_by_year, by_year[, .(posted_year, year_total = published_comments)], by = "posted_year")
top_by_year[, share_of_year := round(published_comments / year_total, 4)]
setnames(top_by_year, "published_comments", "published_comments_in_year")
top_by_year <- top_by_year[order(posted_year, rank_in_year),
                           .(posted_year, rank_in_year, docket_id, published_comments_in_year, share_of_year)]

by_cohort <- ranked[!is.na(docket_year), {
  has <- published_comments[published_comments > 0L]
  c(list(dockets = .N,
         dockets_with_comments = length(has),
         share_zero_comments = round(1 - length(has) / .N, 4),
         published_comments = sum(published_comments),
         median_per_docket = median(as.numeric(published_comments)),
         median_per_commented_docket = if (length(has)) median(as.numeric(has)) else 0,
         p90_per_commented_docket = if (length(has)) quantile(as.numeric(has), 0.9, names = FALSE) else 0,
         max_docket = docket_id[which.max(published_comments)],
         max_docket_comments = max(published_comments)),
    threshold_counts(published_comments))
}, by = docket_year][order(docket_year)]

long_table <- copy(dated)

legacy <- ranked[!is.na(docket_year) & !is.na(year_of(first_posted)) & year_of(first_posted) < docket_year]
no_year <- ranked[is.na(docket_year)]
exclusions <- rbind(exclusions,
  data.table(rule = c("kept, flagged: first posted before the year in the docket ID (legacy uploads)",
                      "kept, flagged: no docket year could be assigned"),
             dockets = c(nrow(legacy), nrow(no_year)),
             comments = c(sum(legacy$published_comments), sum(no_year$published_comments)),
             share_of_dockets = round(c(nrow(legacy), nrow(no_year)) / nrow(ranked), 5),
             share_of_comments = round(c(sum(legacy$published_comments), sum(no_year$published_comments)) /
                                         sum(ranked$published_comments), 5)))

write_table(ranked, "rulemaking_dockets_ranked.csv")
write_table(exclusions, "data_exclusions.csv")
write_table(by_year, "published_by_year.csv")
write_table(by_cohort, "published_by_docket_year.csv")
write_table(top_by_year, "top_dockets_by_year.csv")
write_table(long_table, "published_by_docket_and_year.csv")

if (topup_comments > 0 || topup_dockets > 0) {
  cat(sprintf("includes top-ups: %s comments from the raw bucket, %d new dockets added by the top-ups\n",
              format(topup_comments, big.mark = ","), topup_dockets))
}
if (api_comments > 0) {
  cat(sprintf("includes %s comments from the API refresh of open dockets\n", format(api_comments, big.mark = ",")))
}
print(exclusions)
cat(sprintf("dockets marked open for comment: %d\n", sum(!is.na(ranked$comment_end_date))))
cat(sprintf("rulemaking dockets: %s total, %s with zero published comments\n",
            format(nrow(ranked), big.mark = ","), format(sum(ranked$published_comments == 0L), big.mark = ",")))
cat(sprintf("published comments: %s (%s with no usable posted date and %s dated before their docket existed, both left out of the by-year tables)\n",
            format(sum(ranked$published_comments), big.mark = ","),
            format(undated_comments, big.mark = ","), format(backfilled_comments, big.mark = ",")))
cat("top 10 dockets:\n")
for (i in 1:10) {
  r <- ranked[i]
  cat(sprintf("  %-24s %-46s %10s\n", r$docket_id, substr(r$docket_title, 1, 44), format(r$published_comments, big.mark = ",")))
}

if (site_only) {
  cat("\nwrote the tables behind the page to", out_dir, "\n")
  quit(save = "no", status = 0)
}

suppressPackageStartupMessages({
  library(ggplot2)
  library(patchwork)
})

tir_theme <- theme_minimal(base_size = 11) +
  theme(plot.background = element_rect(fill = SURFACE, colour = NA),
        panel.background = element_rect(fill = SURFACE, colour = NA),
        panel.grid.major.x = element_blank(),
        panel.grid.minor = element_blank(),
        panel.grid.major.y = element_line(colour = GRID, linewidth = 0.4),
        axis.line = element_line(colour = MUTED, linewidth = 0.4),
        axis.ticks = element_line(colour = MUTED, linewidth = 0.4),
        axis.text = element_text(colour = INK2, size = 9),
        axis.title = element_blank(),
        plot.title = element_text(face = "bold", colour = INK, size = 12),
        plot.title.position = "plot",
        legend.title = element_blank(),
        legend.text = element_text(colour = INK2))

no_x_axis <- theme(axis.text.x = element_blank(), axis.ticks.x = element_blank(), axis.line.x = element_blank())

caption_theme <- theme(plot.background = element_rect(fill = SURFACE, colour = NA),
                       plot.caption = element_text(colour = MUTED, size = 8, hjust = 0))

foot <- sprintf("Published = comments posted on regulations.gov (Spicy Regs mirror). %d is year to date.", THIS_YEAR)
x_breaks <- seq(ceiling(chart_from / 5) * 5, THIS_YEAR, by = 5)

save_png <- function(plot, name, width, height) {
  ggsave(file.path(out_dir, name), plot, width = width, height = height, dpi = 150,
         device = ragg::agg_png, bg = SURFACE)
}

by <- by_year[posted_year >= chart_from]
vol <- melt(by[, .(posted_year, partial_year,
                   `All other dockets` = all_other_comments,
                   `Ten largest dockets that year` = top10_comments)],
            id.vars = c("posted_year", "partial_year"), variable.name = "group", value.name = "n")
vol[, group := factor(group, levels = c("Ten largest dockets that year", "All other dockets"))]
ytd <- by[posted_year == THIS_YEAR]

f1_top <- ggplot(vol, aes(posted_year, n, fill = group, alpha = partial_year)) +
  geom_col(width = 0.75) +
  scale_fill_manual(values = c("Ten largest dockets that year" = ORANGE, "All other dockets" = BLUE)) +
  scale_alpha_manual(values = c("FALSE" = 1, "TRUE" = 0.5), guide = "none") +
  guides(fill = guide_legend(reverse = TRUE)) +
  scale_x_continuous(limits = c(chart_from - 0.8, THIS_YEAR + 0.8), breaks = x_breaks, expand = c(0, 0)) +
  scale_y_continuous(labels = fmt_si, expand = expansion(mult = c(0, 0.1))) +
  ggtitle("Published comments on rulemaking dockets, by year posted") +
  tir_theme + no_x_axis +
  theme(legend.position = "inside", legend.position.inside = c(0.02, 0.98), legend.justification = c(0, 1))
if (nrow(ytd)) {
  f1_top <- f1_top + annotate("text", x = THIS_YEAR, y = ytd$published_comments, vjust = -0.5,
                              colour = INK2, size = 3, label = "YTD")
}

f1_bottom <- ggplot(by, aes(posted_year, active_dockets)) +
  geom_line(colour = BLUE, linewidth = 0.9) +
  geom_point(colour = BLUE, size = 1.6) +
  scale_x_continuous(limits = c(chart_from - 0.8, THIS_YEAR + 0.8), breaks = x_breaks, expand = c(0, 0)) +
  scale_y_continuous(labels = fmt_si, limits = c(0, NA), expand = expansion(mult = c(0, 0.08))) +
  ggtitle("Rulemaking dockets receiving at least one published comment") +
  tir_theme + theme(plot.title = element_text(face = "plain", colour = INK, size = 11))

fig1 <- (f1_top / f1_bottom) + plot_layout(heights = c(3, 2)) +
  plot_annotation(caption = foot, theme = caption_theme)
save_png(fig1, "fig1_published_per_year.png", 10, 8)

co <- by_cohort[docket_year >= chart_from & docket_year <= THIS_YEAR]
last_co <- co[.N]
co_pos <- co[median_per_commented_docket > 0 & p90_per_commented_docket > 0]

f2_top <- ggplot(co_pos, aes(docket_year)) +
  geom_line(aes(y = median_per_commented_docket), colour = BLUE, linewidth = 0.9) +
  geom_point(aes(y = median_per_commented_docket), colour = BLUE, size = 1.6) +
  geom_line(aes(y = p90_per_commented_docket), colour = ORANGE, linewidth = 0.9) +
  geom_point(aes(y = p90_per_commented_docket), colour = ORANGE, size = 1.6) +
  annotate("text", x = last_co$docket_year + 0.5, y = last_co$p90_per_commented_docket, hjust = 0,
           colour = INK2, size = 3.2, label = "90th percentile") +
  annotate("text", x = last_co$docket_year + 0.5, y = last_co$median_per_commented_docket, hjust = 0,
           colour = INK2, size = 3.2, label = "Median") +
  scale_x_continuous(limits = c(chart_from - 0.8, THIS_YEAR + 4), breaks = x_breaks, expand = c(0, 0)) +
  scale_y_log10(limits = c(2, 1500), breaks = c(3, 10, 30, 100, 300, 1000), labels = fmt_si) +
  ggtitle("Published comments per rulemaking docket (dockets with at least one), by year docket opened") +
  tir_theme + no_x_axis

f2_bottom <- ggplot(co, aes(docket_year, 100 * share_zero_comments)) +
  geom_line(colour = BLUE, linewidth = 0.9) +
  geom_point(colour = BLUE, size = 1.6) +
  scale_x_continuous(limits = c(chart_from - 0.8, THIS_YEAR + 4), breaks = x_breaks, expand = c(0, 0)) +
  scale_y_continuous(limits = c(0, 100)) +
  ggtitle("Share of rulemaking dockets with no published comments (%)") +
  tir_theme + theme(plot.title = element_text(face = "plain", colour = INK, size = 11))

fig2 <- (f2_top / f2_bottom) + plot_layout(heights = c(3, 2)) +
  plot_annotation(caption = paste(foot, "Dockets are dated by the year in their ID."), theme = caption_theme)
save_png(fig2, "fig2_typical_docket.png", 10, 8)

threshold_labels <- paste0(formatC(THRESHOLDS, big.mark = ",", format = "d"), "+ published comments")
large <- melt(co[, c("docket_year", paste0("dockets_ge_", THRESHOLDS)), with = FALSE],
              id.vars = "docket_year", variable.name = "threshold", value.name = "dockets")
large[, threshold := factor(threshold, levels = paste0("dockets_ge_", THRESHOLDS), labels = threshold_labels)]
large_last <- large[docket_year == max(docket_year)]
large_last[, end_label := paste0(formatC(THRESHOLDS, big.mark = ",", format = "d"), "+")]

fig3 <- ggplot(large, aes(docket_year, dockets, colour = threshold)) +
  geom_line(linewidth = 0.9) +
  geom_point(size = 1.6) +
  geom_text(data = large_last, aes(label = end_label), hjust = 0, nudge_x = 0.4, colour = INK2, size = 3.2,
            show.legend = FALSE) +
  scale_colour_manual(values = setNames(c(BLUE, ORANGE, INK2), threshold_labels)) +
  scale_x_continuous(limits = c(chart_from - 0.8, THIS_YEAR + 3.6), breaks = x_breaks, expand = c(0, 0)) +
  scale_y_continuous(limits = c(0, NA), expand = expansion(mult = c(0, 0.08))) +
  ggtitle("Rulemaking dockets drawing large public response, by year docket opened") +
  labs(caption = paste(foot, "Lifetime published count per docket.")) +
  tir_theme +
  theme(plot.caption = element_text(colour = MUTED, size = 8, hjust = 0),
        legend.position = "inside", legend.position.inside = c(0.02, 0.98), legend.justification = c(0, 1))
save_png(fig3, "fig3_large_dockets.png", 10, 5.5)

EM_COEF <- c(1 / 12, -1 / 720, 1 / 30240, -1 / 1209600, 1 / 47900160, -691 / 1307674368000)

hzeta <- function(s, q) {
  n_terms <- 12L
  head_sum <- rowSums(outer(q, 0:(n_terms - 1L), "+")^(-s))
  qn <- q + n_terms
  poch <- s
  corr <- 0
  for (j in seq_along(EM_COEF)) {
    corr <- corr + EM_COEF[j] * poch * qn^(-s - 2 * j + 1)
    poch <- poch * (s + 2 * j - 1) * (s + 2 * j)
  }
  head_sum + qn^(1 - s) / (s - 1) + qn^(-s) / 2 + corr
}

pl_alpha <- function(x, xmin) {
  t <- x[x >= xmin]
  n <- length(t)
  slx <- sum(log(t))
  nll <- function(a) a * slx + n * log(hzeta(a, xmin))
  optimize(nll, c(1.05, 8), tol = 1e-8)$minimum
}

pl_cdf <- function(a, xmin, v) 1 - hzeta(a, v + 1) / hzeta(a, xmin)

ks_stat <- function(x, xmin, a) {
  t <- sort(x[x >= xmin])
  n <- length(t)
  runs <- rle(t)
  u <- runs$values
  hi <- cumsum(runs$lengths) / n
  lo <- hi - runs$lengths / n
  max(abs(hi - pl_cdf(a, xmin, u)), abs(lo - pl_cdf(a, xmin, u - 1)))
}

fit_powerlaw <- function(x) {
  x <- as.numeric(x)
  sx <- sort(x)
  uniq <- unique(sx[sx >= 1])
  n_ge <- length(sx) - findInterval(uniq - 0.5, sx)
  cand <- uniq[n_ge >= MIN_TAIL]
  if (!length(cand)) return(NULL)
  if (length(cand) > MAX_XMIN) {
    idx <- round(seq(0, 1, length.out = MAX_XMIN) * (length(cand) - 1)) + 1
    cand <- unique(cand[idx])
  }
  best <- NULL
  for (xm in cand) {
    a <- pl_alpha(x, xm)
    d <- ks_stat(x, xm, a)
    if (is.null(best) || d < best$ks) best <- list(alpha = a, xmin = xm, ks = d)
  }
  c(best, list(n_tail = sum(x >= best$xmin), n = length(x)))
}

vuong <- function(ll_pl, ll_alt) {
  d <- ll_pl - ll_alt
  s <- sqrt(mean((d - mean(d))^2))
  if (s == 0) return(c(0, 1))
  z <- sum(d) / (sqrt(length(d)) * s)
  c(sum(d), 2 * pnorm(-abs(z)))
}

compare_tails <- function(x, fit) {
  xm <- fit$xmin
  t <- as.numeric(x[x >= xm])
  ll_pl <- -fit$alpha * log(t) - log(hzeta(fit$alpha, xm))
  lt <- log(t)
  ln_logpmf <- function(p) {
    s <- exp(p[2])
    upper <- function(v) pnorm((log(v) - p[1]) / s, lower.tail = FALSE)
    pmf <- (upper(t - 0.5) - upper(t + 0.5)) / upper(xm - 0.5)
    log(pmax(pmf, 1e-300))
  }
  ln_nll <- function(p) -sum(ln_logpmf(p))
  ln_control <- list(reltol = 1e-12, maxit = 5000)
  ln_fit <- optim(c(mean(lt), log(sqrt(mean((lt - mean(lt))^2)) + 0.5)), ln_nll, method = "Nelder-Mead",
                  control = ln_control)
  ln_fit <- optim(ln_fit$par, ln_nll, method = "Nelder-Mead", control = ln_control)
  ex_logpmf <- function(l) log(1 - exp(-l)) - l * (t - xm)
  lambda <- optimize(function(l) -sum(ex_logpmf(l)), c(1e-6, 5), tol = 1e-8)$minimum
  ln_res <- vuong(ll_pl, ln_logpmf(ln_fit$par))
  ex_res <- vuong(ll_pl, ex_logpmf(lambda))
  list(R_lognormal = ln_res[1], p_lognormal = ln_res[2], R_exponential = ex_res[1], p_exponential = ex_res[2])
}

gof_pvalue <- function(x, fit, reps) {
  xm <- fit$xmin
  a <- fit$alpha
  n <- length(x)
  body <- x[x < xm]
  p_tail <- fit$n_tail / n
  cdf <- pl_cdf(a, xm, xm + 0:99999)
  worse <- 0L
  for (b in seq_len(reps)) {
    k <- rbinom(1L, n, p_tail)
    u <- runif(k)
    idx <- findInterval(u, cdf, left.open = TRUE)
    tail_draw <- xm + idx
    beyond <- idx >= length(cdf)
    tail_draw[beyond] <- floor((xm - 0.5) * (1 - u[beyond])^(-1 / (a - 1)) + 0.5)
    resampled <- if (length(body)) body[sample.int(length(body), n - k, replace = TRUE)] else numeric(0)
    f <- fit_powerlaw(c(tail_draw, resampled))
    if (!is.null(f) && f$ks >= fit$ks) worse <- worse + 1L
  }
  worse / reps
}

set.seed(20261030)

pos <- ranked[published_comments > 0L & !is.na(docket_year)]

cohort_fits <- rbindlist(lapply(sort(unique(pos$docket_year)), function(yr) {
  x <- pos[docket_year == yr, published_comments]
  f <- fit_powerlaw(x)
  if (is.null(f)) return(NULL)
  c(list(docket_year = yr), f, compare_tails(x, f),
    list(n_commented = length(x), alpha_se = (f$alpha - 1) / sqrt(f$n_tail)))
}))
setcolorder(cohort_fits, c("docket_year", "alpha", "xmin", "ks", "n_tail", "n", "R_lognormal", "p_lognormal",
                           "R_exponential", "p_exponential", "n_commented", "alpha_se"))

era_spans <- c(ERAS, list(c(1970L, THIS_YEAR)))
era_fits <- rbindlist(lapply(era_spans, function(span) {
  x <- pos[docket_year >= span[1] & docket_year <= span[2], published_comments]
  f <- fit_powerlaw(x)
  gof <- if (n_boot > 0) gof_pvalue(x, f, n_boot) else NA_real_
  c(list(era = if (identical(span, c(1970L, THIS_YEAR))) "all years" else sprintf("%d-%d", span[1], span[2])),
    f, compare_tails(x, f),
    list(gof_p = gof, alpha_se = (f$alpha - 1) / sqrt(f$n_tail), n_commented = length(x),
         tail_share_of_comments = sum(x[x >= f$xmin]) / sum(x)))
}))

write_table(round_table(copy(cohort_fits)), "powerlaw_fit_by_cohort_year.csv")
write_table(round_table(copy(era_fits)), "powerlaw_fit_by_era.csv")

options(width = 200)
print(round_table(copy(era_fits[, .(era, n_commented, alpha, alpha_se, xmin, n_tail, ks, gof_p, R_lognormal,
                                    p_lognormal, R_exponential, p_exponential, tail_share_of_comments)]), 3))
print(round_table(copy(cohort_fits[, .(docket_year, n_commented, alpha, alpha_se, xmin, n_tail, ks,
                                       R_lognormal, p_lognormal)]), 3))

era_palette <- colorRampPalette(c("#9ecae1", "#08306b"))(length(ERAS))
era_names <- vapply(ERAS, function(s) sprintf("%d–%d", s[1], s[2]), "")
ccdf <- rbindlist(lapply(seq_along(ERAS), function(i) {
  x <- sort(pos[docket_year >= ERAS[[i]][1] & docket_year <= ERAS[[i]][2], published_comments])
  data.table(era = era_names[i], x = x, ccdf = 1 - (seq_along(x) - 1) / length(x))
}))
ccdf[, era := factor(era, levels = era_names)]

all_fit <- era_fits[era == "all years"]
ref_label <- sprintf("power-law reference, α≈%.2f", all_fit$alpha)
ref <- data.table(x = 10^seq(log10(all_fit$xmin), 5.9, length.out = 50))
ref[, ccdf := (x / all_fit$xmin)^(1 - all_fit$alpha) * all_fit$n_tail / all_fit$n_commented]
ref[, era := ref_label]
ccdf_colours <- setNames(c(era_palette, ORANGE), c(era_names, ref_label))

f4_left <- ggplot() +
  geom_line(data = ccdf, aes(x, ccdf, colour = era), linewidth = 0.9) +
  geom_line(data = ref, aes(x, ccdf, colour = era), linewidth = 0.8, linetype = "dashed") +
  scale_colour_manual(values = ccdf_colours, breaks = names(ccdf_colours)) +
  guides(colour = guide_legend(override.aes = list(linetype = c(rep("solid", length(ERAS)), "dashed")))) +
  scale_x_log10(breaks = 10^(0:6), labels = fmt_si) +
  scale_y_log10(breaks = 10^(-3:0), labels = c("0.001", "0.01", "0.1", "1")) +
  labs(x = "Published comments on a docket", y = "Share of commented dockets with at least this many") +
  ggtitle("Docket size, by era docket opened") +
  tir_theme +
  theme(axis.title = element_text(colour = INK2, size = 9), plot.title = element_text(face = "plain", size = 12),
        legend.position = "inside", legend.position.inside = c(0.98, 0.98), legend.justification = c(1, 1),
        legend.text = element_text(size = 8.5), legend.key.width = unit(1.6, "lines"),
        panel.grid.major.x = element_line(colour = GRID, linewidth = 0.4))

alpha_years <- cohort_fits[n_tail >= 50]
f4_right <- ggplot(alpha_years, aes(docket_year, alpha)) +
  annotate("rect", xmin = -Inf, xmax = Inf, ymin = 2, ymax = 3, fill = ORANGE, alpha = 0.1) +
  annotate("text", x = min(alpha_years$docket_year), y = 2.97, hjust = 0, vjust = 1, colour = MUTED, size = 3,
           label = "typical 2–3 range for heavy-tailed data") +
  geom_errorbar(aes(ymin = alpha - 1.96 * alpha_se, ymax = alpha + 1.96 * alpha_se), colour = MUTED,
                width = 0.35, linewidth = 0.4) +
  geom_line(colour = BLUE, linewidth = 0.7) +
  geom_point(colour = BLUE, size = 1.8) +
  scale_x_continuous(breaks = seq(1995, THIS_YEAR, by = 5)) +
  scale_y_continuous(limits = c(NA, 3.05)) +
  labs(x = "Year docket opened", y = "Fitted exponent α (tail, ±95% CI)") +
  ggtitle("Tail exponent over time (cohorts with 50+ tail dockets)") +
  tir_theme +
  theme(axis.title = element_text(colour = INK2, size = 9), plot.title = element_text(face = "plain", size = 12),
        panel.grid.major.x = element_line(colour = GRID, linewidth = 0.4))

fig4 <- (f4_left | f4_right) + plot_annotation(theme = caption_theme)
save_png(fig4, "fig4_power_law.png", 11.5, 4.6)
