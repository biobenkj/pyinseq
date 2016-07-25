#!/usr/bin/env python

'''Main script for running the pyinseq package.'''

import argparse
import csv
import glob
import logging
import os
import pandas as pd
import regex as re
import screed
import sys
import yaml
from shutil import copyfile
from collections import OrderedDict
from gbkconvert import gbk2fna, gbk2ftt
from mapReads import bowtieBuild, bowtieMap, parseBowtie
from processMapping import mapSites, mapGenes, buildGeneTable
from utils import convert_to_filename, createExperimentDirectories
# from demultiplex import demultiplex_fastq, trim_fastq

logging.basicConfig(level=logging.INFO)
logger = logging.getLogger(__name__)


def parseArgs(args):
    '''Parse command line arguments.'''
    parser = argparse.ArgumentParser()
    parser.add_argument('-i', '--input',
                        help='input Illumina reads file or folder',
                        required=True)
    parser.add_argument('-s', '--samples',
                        help='sample list with barcodes. \
                        If not provided then entire folder provided for --input is analyzed',
                        required=False)
    parser.add_argument('-e', '--experiment',
                        help='experiment name (no spaces or special characters)',
                        required=True)
    parser.add_argument('-g', '--genome',
                        help='genome in GenBank format (one concatenated file for multiple contigs/chromosomes)',
                        required=True)
    parser.add_argument('-d', '--disruption',
                        help='fraction of gene disrupted (0.0 - 1.0)',
                        default=1.0)
    parser.add_argument('--nobarcodes',
                        help='barcodes have already been removed from the samples; \
                        -i should list the directory with filenames (.fastq.gz) \
                        corresponding to the sample names',
                        action='store_true',
                        default=False)
    parser.add_argument('--demultiplex',
                        help='demultiplex initial file into separate files by barcode',
                        action='store_true',
                        default=False)
    parser.add_argument('--compress',
                        help='compress (gzip) demultiplexed samples',
                        action='store_true',
                        default=False)
    parser.add_argument('--keepall',
                        help='keep all intermediate files generated',
                        action='store_true',
                        default=False)
    return parser.parse_args(args)


class cd:
    '''Context manager to change to the specified directory then back.'''
    def __init__(self, newPath):
        self.newPath = os.path.expanduser(newPath)

    def __enter__(self):
        self.savedPath = os.getcwd()
        os.chdir(self.newPath)

    def __exit__(self, etype, value, traceback):
        os.chdir(self.savedPath)


class Settings():
    # name for folder
    experiment = 'undefined'
    # path for samples.yml file
    samples_yaml = 'undefined'
    # path for summary.yml file
    summary_yaml = 'undefined'
    # keep all intermediate files
    keepall = False
    # samples dictionary (data for each barcoded sample)
    samplesDict = {}
    # summary dictionary (number of reads, etc.)
    summaryDict = {}
    # change to 0 if samples are already demultiplexed with barcode removed
    barcode_length = 4

    def __init__(self):
        pass


def set_paths(experiment_name):
    experiment = convert_to_filename(experiment_name)
    samples_yaml = 'results/{}/samples.yml'.format(experiment)
    summary_yaml = 'results/{}/summary.yml'.format(experiment)
    path = {'experiment': experiment,
            'samples_yaml': samples_yaml,
            'summary_yaml': summary_yaml}
    return path


def set_disruption(d):
    '''Check that gene disrution is 0.0 to 1.0; otherwise set to 1.0'''
    if d < 0.0 or d > 1.0:
        logger.error('Disruption value provided ({0}) is not in range 0.0 to 1.0; proceeding with default value of 1.0'.format(d))
        d = 1.0
    return d


def tab_delimited_samples_to_dict(sample_file):
    '''Read sample names, barcodes from tab-delimited into an OrderedDict.'''
    samplesDict = OrderedDict()
    with open(sample_file, 'r', newline='') as csvfile:
        for line in csv.reader(csvfile, delimiter='\t'):
            if not line[0].startswith('#'):  # ignore comment lines in original file
                # sample > filename-acceptable string
                # barcode > uppercase
                sample = convert_to_filename(line[0])
                barcode = line[1].upper()
                if sample not in samplesDict and barcode not in samplesDict.values():
                        samplesDict[sample] = {'barcode': barcode}
                else:
                    raise IOError('Error: duplicate sample {0} barcode {1}'.format(sample, barcode))
    return samplesDict


def yaml_samples_to_dict(sample_file):
    '''Read sample names, barcodes from yaml into an OrderedDict.'''
    samplesDict = OrderedDict()  # Does this line do anything?
    samplesDict = yaml.load(sample_file)
    return samplesDict


def directory_of_samples_to_dict(directory):
    '''Read sample names from a directory of .gz files into an OrderedDict.'''
    samplesDict = OrderedDict()
    for gzfile in list_files(directory):
        # TODO(convert internal periods to underscore? use regex?)
        # extract file name before any periods
        f = (os.path.splitext(os.path.basename(gzfile))[0].split('.')[0])
        samplesDict[f] = {}
    return samplesDict


def list_files(folder, ext='gz'):
    '''Return list of .gz files in specified folder'''
    with cd(folder):
        return [f for f in glob.glob('*.{}'.format(ext))]


def extract_chromosome_sequence(fastq_file):
    '''
    Return a dictionary of all insertions in a file

    {barcode1:
        {chrom_seq1: count,
         chrom_seq2: count},
     barcode2:
        {chrom_seq1: count,
         chrom_seq2: count}}
    '''
    insertionDict = {}
    pattern = re.compile('''
    ^                               # beginning of string
    ([ACGT]{4})                     # group(1) = barcode, any 4-bp of mixed ACGT
    ([NACGT][ACGT]{13,14}(?:TA))    # group(2) = 16-17 bp of chromosomal sequence
                                    # first bp can be N
                                    # last two must be TA for transposon
    ACAGGTTG                        # flanking transposon sequence
    ''', re.VERBOSE)
    logger.debug('fastq_file: {0}'.format(fastq_file))
    with screed.open(fastq_file) as seqfile:
        for read in seqfile:
            m = re.search(pattern, read.sequence)
            try:
                barcode, chrom_seq = m.group(1), m.group(2)
                try:
                    insertionDict.setdefault(barcode, {chrom_seq: 0})[chrom_seq] += 1
                # insertionDict[barcode] exists but not {chrom_seq: ...} nested dict
                except(KeyError):
                    insertionDict[barcode][chrom_seq] = 1
            # no barcode/chrom_seq in the read
            except:
                pass
    logger.debug('insertionDict: {0}'.format(insertionDict))
    return insertionDict


def map_raw_reads(insertionDict, samplesDict, organism, genomeDir):
    '''
    Map sequences with bowtie.

    Iterate through each barcode in insertionDict and map reads using bowtie.
    Map only reads for barcodes in the samples dictionary (i.e., barcodes that are
    in the experiment)

    Input dictionary is a nested dictionary defined in extract_chromosome_sequence():

    {barcode1:
        {chrom_seq1: count,
         chrom_seq2: count},
     barcode2:
        {chrom_seq1: count,
         chrom_seq2: count}}
    '''
    with cd(genomeDir):
        for sample in samplesDict:
            if samplesDict[sample]['barcode'] in insertionDict:
                # for read in insertionDict[samplesDict[sample]['barcode']]:
                # bowtie_in = list(insertionDict[samplesDict[sample]['barcode']].keys())
                bowtie_in = ','.join([read for read in insertionDict[samplesDict[sample]['barcode']]])
                logger.info('bowtie mapping for sample: {0}: {1}'.format(sample, samplesDict[sample]))
                logger.debug('samples_for_bowtie_mapping: {0}'.format(bowtie_in))
                bowtie_out = '../{0}_results_bowtie.txt'.format(sample)
                # return the bowtie message for parsing and analysis
                logger.debug('bowtie commands: {0}, {1}, {2}'.format(organism, bowtie_in, bowtie_out))
                bowtie_msg_out = bowtieMap(organism, bowtie_in, bowtie_out)


def yamldump(d, f):
    '''Write dictionary d as yaml file f.yml'''
    with open(f + '.yml', 'w') as fo:
        fo.write(yaml.dump(d, default_flow_style=False))

def process_bowtie_results(insertionDict, samplesDict, organism):
    '''Read each bowtie result file into a dataframe, align with read data

       For each results file (columns 1,6,7 suppressed):
       - Read into a pandas dataframe
       - Align to the insertion data (number of reads)
       - Write to a csv/txt file
       - Concatenate the results from all of the samples

    '''
    for sample in samplesDict:
        bowtie_file = 'results/{experiment}/{sample}_bowtie.txt'.format(
            experiment=path['experiment'],
            sample=sample)


        if samplesDict[sample]['barcode'] in insertionDict:
            # for read in insertionDict[samplesDict[sample]['barcode']]:
            # bowtie_in = list(insertionDict[samplesDict[sample]['barcode']].keys())
            bowtie_in = ','.join([read for read in insertionDict[samplesDict[sample]['barcode']]])
            logger.info('bowtie mapping for sample: {0}: {1}'.format(sample, samplesDict[sample]))
            logger.debug('samples_for_bowtie_mapping: {0}'.format(bowtie_in))
            bowtie_out = '../{0}_results_bowtie.txt'.format(sample)
            # return the bowtie message for parsing and analysis
            logger.debug('bowtie commands: {0}, {1}, {2}'.format(organism, bowtie_in, bowtie_out))
            bowtie_msg_out = bowtieMap(organism, bowtie_in, bowtie_out)
    # df_bowtie_results =

### Try doing the bowtie mapping with:
###
### yamldump a **SORTED** yaml of dictionaries for each real barcode and then
### a yaml for all of the ones that are not part of any barcode?
###
### -c to pass a list of reads (sorted)
### --suppress to suppress output. Or will I need the reads to map back b/c dictionaries unordered?





def pipeline_organize(barcodes_present, samples, source=''):

    # add 'demultiplexedPath' and 'trimmedPath' fields for each sample
    for sample in Settings.samplesDict:
        demultiplexedPath = 'results/{experiment}/raw_data/{sampleName}.fastq.gz'.format(
            experiment=Settings.experiment,
            sampleName=Settings.samplesDict[sample]['name'])
        trimmedPath = 'results/{experiment}/{sampleName}_trimmed.fastq'.format(
            experiment=Settings.experiment,
            sampleName=Settings.samplesDict[sample]['name'])
        Settings.samplesDict[sample]['demultiplexedPath'] = demultiplexedPath
        Settings.samplesDict[sample]['trimmedPath'] = trimmedPath
        if not samples:
            # copy demultiplexed files to the demultiplexed path
            src = source + '/' + sample + '.fastq.gz'
            dst_file = Settings.samplesDict[sample]['demultiplexedPath']
            dst = os.path.dirname(dst_file)
            print('src', src, 'dst', dst)
            copyfile(src, dst_file)

    print('\nProcessing {} total samples:'.format(len(Settings.samplesDict)))
    try:
        for s in Settings.samplesDict:
            print('{0}\n  barcode: {1}'.format(s, Settings.samplesDict[s]['barcode']))
    except:
        pass
    with open(Settings.samples_yaml, 'w') as fo:
        fo.write(yaml.dump(Settings.samplesDict, default_flow_style=False))
    print('Sample details written to {}'.format(Settings.samples_yaml))


def parse_genbank_setup_bowtie(gbkfile, organism, genomeDir, disruption):
    logger.info('Preparing nucleotide fasta file from GenBank file to use in bowtie mapping.\n' \
        '  GenBank source file: {}'.format(gbkfile))
    gbk2fna(gbkfile, organism, genomeDir)
    logger.info('Preparing feature table file from GenBank file to use in gene mapping.\n' \
        '  GenBank source file: {}'.format(gbkfile))
    gbk2ftt(gbkfile, organism, genomeDir)
    # Change directory, build bowtie indexes, change directory back
    with cd(genomeDir):
        logger.info('Building bowtie index files in results/{}/genome_lookup'.format(Settings.experiment))
        bowtieBuild(organism)


def left_over_from_old_mapping_module():

    # Dictionary of each sample's cpm by gene
    geneMappings = {}
    for sample in Settings.samplesDict:
        s = Settings.samplesDict[sample]
        print('\nProcessing sample {}'.format(sample))
        sample_reads, trimmed_reads = trim_fastq(s['demultiplexedPath'], s['trimmedPath'],
                                                 sample, Settings.barcode_length)
        Settings.samplesDict[sample]['reads_with_bc'] = sample_reads
        Settings.samplesDict[sample]['reads_with_bc_seq_tn'] = trimmed_reads
        # Change directory, map to bowtie, change directory back
        trimmedSampleFile = '{0}_trimmed.fastq'.format(sample)
        bowtieOutputFile = '{0}_bowtie.txt'.format(sample)
        with cd(genomeDir):
            # Paths are relative to the genome_lookup directory
            # from where bowtie is called
            bowtie_in = '../{0}'.format(trimmedSampleFile)
            bowtie_out = '../{0}'.format(bowtieOutputFile)
            # map to bowtie and produce the output file
            print('\nMapping {} reads with bowtie'.format(sample))
            bowtie_msg_out = bowtieMap(organism, bowtie_in, bowtie_out)
            # store bowtie data for each sample in dictionary
            Settings.samplesDict[sample]['bowtie_results'] = parseBowtie(bowtie_msg_out)
        # Map each bowtie result to the chromosome
        insertions = len(mapSites('results/{0}/{1}'.format(Settings.experiment, bowtieOutputFile)))
        Settings.samplesDict[sample]['insertion_sites'] = insertions
        # Add gene-level results for the sample to geneMappings
        # Filtered on gene fraction disrupted as specified by -d flag
        geneMappings[sample] = mapGenes(organism, sample, disruption, Settings.experiment)
        if not Settings.keepall:
            # Delete trimmed fastq file, bowtie mapping file after writing mapping results
            os.remove(s['trimmedPath'])
            os.remove('results/{0}/{1}'.format(Settings.experiment, bowtieOutputFile))
    buildGeneTable(organism, Settings.samplesDict, geneMappings, Settings.experiment)
    # print(logdata)


def pipeline_analysis():

    print('\n===================='
          '\n*     Analysis     *'
          '\n====================\n')

    # write samples.yml with data for each sample
    print('Writing file with summary data for each sample:\n  {}'.format(Settings.samples_yaml))
    print(yaml.dump(Settings.samplesDict, default_flow_style=False))
    with open(Settings.samples_yaml, 'w') as fo:
        fo.write(yaml.dump(Settings.samplesDict, default_flow_style=False))

    # write summary.yml with data for entire experiment
    print('Writing file with overall summary information:\n  {}'.format(Settings.summary_yaml))
    print(yaml.dump(Settings.summaryDict, default_flow_style=False))
    with open(Settings.summary_yaml, 'w') as fo:
        fo.write(yaml.dump(Settings.summaryDict, default_flow_style=False))


def main():
    logger.info('Process command line arguments')
    args = parseArgs(sys.argv[1:])
    # Keep intermediate files?
    Settings.keepall = args.keepall
    gbkfile = args.genome
    reads = args.input
    disruption = set_disruption(float(args.disruption))
    # Organism reference files called 'genome.fna' etc
    organism = 'genome'
    # sample names and paths
    samples = args.samples
    barcodes_present = not args.nobarcodes
    if samples:
        samplesDict = tab_delimited_samples_to_dict(samples)
    else:
        reads = os.path.abspath(reads)
        samplesDict = directory_of_samples_to_dict(samples)
    logger.debug('samplesDict: {0}'.format(samplesDict))

    # --- SET UP DIRECTORIES --- #
    path = set_paths(args.experiment)
    createExperimentDirectories(path['experiment'])
    # TODO(need to clean up so that samplesDict and summaryDict have everything; get rid of path{})
    # put 'organism' in summaryDict

    # --- SET UP BOWTIE --- #
    genomeDir = 'results/{experiment}/genome_lookup/'.format(experiment=path['experiment'])
    parse_genbank_setup_bowtie(gbkfile, organism, genomeDir, disruption)

    # --- PROCESS INSEQ --- #
    logger.info('Process INSeq samples')
    insertionDict = extract_chromosome_sequence(reads)
    logger.info('Done making insertionDict')
    # DEBUG:
    yamldump(insertionDict,
             'results/{experiment}/insertionDict'.format(experiment=path['experiment']))
    map_raw_reads(insertionDict, samplesDict, path, organism, genomeDir)

    # --- PROCESS BOWTIE MAPPINGS --- #
    logger.info('Process bowtie results')
    process_bowtie_results(insertionDict, samplesDict, organism)

    # TODO(use pandas to integrate the sample-level results and bowtie results!!)
    # :)


    # --- BOWTIE MAPPING --- #

    if not samples:
        Settings.summaryDict['total reads'] = 0
        for sample in Settings.samplesDict:
            print(Settings.samplesDict[sample])
            Settings.summaryDict['total reads'] += Settings.samplesDict[sample]['reads_with_bc']

    # --- ANALYSIS OF RESULTS --- #
    pipeline_analysis()

    # --- CONFIRM ETION --- #
    print('\n===================='
          '\n*       Done       *'
          '\n====================\n')


if __name__ == '__main__':
    main()
